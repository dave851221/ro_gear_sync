"""customtkinter main window for RO_GearSync.

Scope (per user request: "核心分析截圖流程優先"):

  * Env check section — ADB binary, LDPlayer device, OCR engine, Excel path
  * Live scan section — start button, progress, member list with diff colors
  * Workbook actions — 改名/管理成員 dialog, save status
  * Completion summary — counts and "open Excel" / scan again

The full PLAN §8 state machine (改名登記 pre-scan dialog, post-scan
review of MISSED/UNMATCHED with phase-3 alternatives) is **deferred** —
those become add-on dialogs we can drop in later without restructuring.

Threading model:

  * Tk runs on the main thread.
  * :class:`ScanRunner` owns a worker thread that drives :class:`CaptureSession`
    (itself two threads: producer + consumer).
  * The worker pushes events onto a queue. The Tk main loop polls the queue
    every ~50ms via ``after()`` and updates widgets synchronously.

OCR engine warm-up is roughly 10 seconds, so we lazy-load it on the first
scan and keep it for the rest of the session.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from dataclasses import asdict
from datetime import date
from pathlib import Path
from tkinter import Menu, filedialog, messagebox

import customtkinter as ctk

from ..adb import (
    AdbClient,
    AdbError,
    InstanceProbeResult,
    InstanceStatus,
    find_ldplayer_instances,
)
from ..capture.session import CapturedMember
from ..storage import GuildScoresWorkbook
from ..utils.logging import logger
from ..utils.config import app_config, reload_config
from ..utils.paths import (
    adb_binary,
    config_path,
    default_workbook_path,
    ldconsole_binary,
    user_data_dir,
)
from ..vision import OcrEngine
from .rename_dialog import RenameDialog
from .review_dialog import ReviewDialog
from .save_success_dialog import SaveSuccessDialog
from .scan_runner import LiveMember, ScanRunner, SummaryPayload, resolve_captures


# Colours for the live-scan diff badges. Only the diff text is coloured —
# the gear-score number stays the default foreground per spec.
COLOR_UP = "#1d8a3d"     # green
COLOR_DOWN = "#c1351c"   # red
COLOR_NEUTRAL_FG = ("gray10", "gray90")
COLOR_REVIEW_BG = ("#ffe7e7", "#5a2424")   # light red — fuzzy/unmatched rows
COLOR_PENDING_BG = ("#f0f0f0", "#2a2a2a")  # neutral grey — waiting for fuzzy
COLOR_EXACT_BG = ("#ffffff", "#262626")    # default row background

# Live-scan table column widths. The # and name columns are fixed; the
# gear and delta columns are anchored to the right edge so when the
# user widens the window they slide together with it, keeping the
# scoreboard look. Name column gets weight=1 to absorb any extra space.
_LIVE_IDX_W = 50
_LIVE_NAME_W = 220        # min width — grows when window stretches
_LIVE_GEAR_W = 100
_LIVE_DELTA_W = 100

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

# --- customtkinter bug guard -------------------------------------------
# CTkScrollableFrame binds <MouseWheel> with bind_all. When the wheel
# turns over a Tk-internal widget that has NO Python wrapper (the classic
# case: ttk.Combobox's popdown listbox, which our league review dialog
# uses), tkinter can't resolve the widget name, event.widget stays a
# STRING, and customtkinter's check_if_master_is_canvas crashes with
# "'str' object has no attribute 'master'". Patch the handler to ignore
# such events — the popdown scrolls itself natively anyway.
_orig_mouse_wheel_all = ctk.CTkScrollableFrame._mouse_wheel_all

def _safe_mouse_wheel_all(self, event):  # noqa: ANN001 — tkinter event
    if isinstance(getattr(event, "widget", None), str):
        return None
    return _orig_mouse_wheel_all(self, event)

ctk.CTkScrollableFrame._mouse_wheel_all = _safe_mouse_wheel_all


class RoGearSyncApp(ctk.CTk):
    """The main application window."""

    POLL_INTERVAL_MS = 50

    def __init__(self) -> None:
        super().__init__()
        # Swallow the specific TclError that fires when customtkinter's
        # internal focus animation hits a freshly-destroyed entry (the
        # ``bad window path name ... .!ctkentry.!entry`` traceback we
        # kept seeing after closing review/rename dialogs). All other
        # exceptions still surface normally.
        import tkinter as _tk
        import traceback as _tb
        def _filtered_report(self, exc, val, tb):  # type: ignore[no-untyped-def]
            if isinstance(val, _tk.TclError) and "bad window path name" in str(val):
                return
            _tb.print_exception(exc, val, tb)
        # report_callback_exception is looked up on the instance and on
        # the class; bind to both so subclasses (CTk) don't miss it.
        self.report_callback_exception = _filtered_report.__get__(self, type(self))
        type(self).report_callback_exception = _filtered_report

        self.title("RO_GearSync")
        # Narrower default per spec ("有點太寬了") — the live table fits
        # comfortably in 820, and the env panel + toolbar both wrap fine.
        # User can still drag wider; the table's gear/delta columns are
        # right-anchored so they follow the right edge.
        self.geometry("820x720")
        self.minsize(720, 600)

        # --- runtime state -------------------------------------------------
        self.adb_client: AdbClient | None = None
        self.adb_path: Path | None = None
        self.ldconsole_path: Path | None = None
        self.device_serial: str | None = None
        self.instances: list[InstanceProbeResult] = []
        self._detecting: bool = False
        self.ocr_primary: OcrEngine | None = None
        self.ocr_fallback: OcrEngine | None = None
        self.ocr_loading: bool = False
        # Optional callback fired when the lazy OCR warm-up finishes.
        # The "重新分析截圖歷史 page" menu action sets this so its own worker
        # only kicks off once the engines are ready.
        self._ocr_ready_callback = None  # type: ignore[assignment]
        # Cached re-OCR arguments — when the user picks a folder before
        # OCR is loaded, we stash the path here so the post-warm-up
        # retry doesn't ask them to pick again.
        self._reocr_pending_args = None  # type: ignore[assignment]
        # Flag the worker thread polls between pages to support 中止.
        # Reset to False at every rescan start; set to True when the
        # user confirms the abort confirm dialog.
        self._reocr_cancel = False

        # User-overrideable via [paths] workbook_path in config.ini.
        self.workbook_path: Path = default_workbook_path()
        self.workbook: GuildScoresWorkbook | None = None
        self.runner: ScanRunner | None = None

        # Live-scan table state. Keyed by capture dedup_key so pending
        # rows can be repainted in place when the post-scan fuzzy phase
        # resolves them. ``_live_members`` mirrors the latest LiveMember
        # for each key so the sort logic always has the freshest state.
        self._row_widgets: dict[str, dict] = {}
        self._row_order: list[str] = []
        self._live_members: dict[str, LiveMember] = {}
        # Captured payload (dict shape matching members.json) carried
        # over from the latest scan/rescan so the review-dialog confirm
        # callback can replay it through merge_capture once the user
        # decides what to do with missed/unmatched entries.
        self._pending_captures: list[dict] = []
        self._pending_capture_day: str | None = None
        # Wall-clock perf-counter timestamp set when the user hits
        # 開始掃描 (live or rescan). Used to compute total elapsed
        # time shown when the review dialog opens.
        self._scan_start_perf: float | None = None

        self._build_ui()
        self._build_menu()
        self._load_workbook(self.workbook_path, silent=True)
        # Defer ADB/ldconsole probing until after the window paints once,
        # so the user sees the UI immediately and can tell the app is alive.
        self.after(50, self._refresh_env_status)

    # ============================================================== layout

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        # row 0 = env check (shared by both features), row 1 = feature
        # tabs (expands), row 2 = status bar.
        self.grid_rowconfigure(1, weight=1)

        # ----- env section (shared)
        env_frame = ctk.CTkFrame(self)
        env_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        env_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(env_frame, text="環境檢查", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=(12, 8), pady=(10, 4), sticky="w", columnspan=3
        )

        # adb_label uses justify="left" + wraplength so long paths wrap
        # onto a second line instead of pushing the 重新偵測 button off
        # the right edge of the window.
        self.adb_label = ctk.CTkLabel(
            env_frame, text="ADB： -", anchor="w", justify="left", wraplength=560,
        )
        self.adb_label.grid(row=1, column=0, padx=(12, 8), pady=2, sticky="w", columnspan=2)

        # Stack 重新偵測 + 選擇 LDPlayer 資料夾 vertically in col=2 so
        # the second button only shows up when auto-detect failed. Both
        # share the same right-aligned column the multi-line adb_label
        # accommodates via sticky="ne".
        adb_btn_col = ctk.CTkFrame(env_frame, fg_color="transparent")
        adb_btn_col.grid(row=1, column=2, padx=(8, 12), pady=2, sticky="ne")
        self.detect_btn = ctk.CTkButton(
            adb_btn_col, text="重新偵測", width=170, command=self._refresh_env_status,
        )
        self.detect_btn.grid(row=0, column=0, pady=(0, 4), sticky="e")
        # Manual LDPlayer-folder picker. Hidden by default — only
        # ``_apply_env_results`` un-hides it when adb / ldconsole
        # cannot be located via the built-in search paths.
        self.browse_ld_btn = ctk.CTkButton(
            adb_btn_col, text="選擇 LDPlayer 資料夾…", width=170,
            command=self._browse_ldplayer_dir,
            fg_color="#1f6feb", hover_color="#1958c9",
        )
        self.browse_ld_btn.grid(row=1, column=0, sticky="e")
        self.browse_ld_btn.grid_remove()

        self.device_label = ctk.CTkLabel(env_frame, text="模擬器： -", anchor="w")
        self.device_label.grid(row=2, column=0, padx=(12, 8), pady=2, sticky="w", columnspan=3)

        # Instance picker. Always visible — even with 0 or 1 instance we
        # want the user to see the status hint underneath. CTkOptionMenu
        # can't disable individual items, so when an instance is in a bad
        # state (ADB off / not running) the dropdown still lists it but
        # the start-scan button refuses to proceed.
        instance_row = ctk.CTkFrame(env_frame, fg_color="transparent")
        instance_row.grid(row=3, column=0, padx=(12, 8), pady=2, sticky="ew", columnspan=3)
        ctk.CTkLabel(instance_row, text="使用實例：", width=80, anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        self.instance_var = ctk.StringVar(value="(尚未偵測)")
        self.instance_menu = ctk.CTkOptionMenu(
            instance_row,
            variable=self.instance_var,
            values=["(尚未偵測)"],
            width=360,
            command=self._on_instance_selected,
            state="disabled",
        )
        self.instance_menu.grid(row=0, column=1, padx=(4, 8), sticky="w")
        self.instance_hint = ctk.CTkLabel(
            instance_row, text="", anchor="w", justify="left", wraplength=580
        )
        self.instance_hint.grid(row=1, column=0, columnspan=3, padx=(4, 0), pady=(2, 0), sticky="w")

        # OCR engine is loaded lazily on first scan; we surface its
        # state via the status bar / scan_status_label rather than a
        # dedicated environment label (the latter just nags before the
        # user clicks 開始掃描).

        # ----- feature tabs: 裝備評分 / 聯賽評分 -------------------------
        # Everything gear-specific (Excel row, toolbar, live table) lives
        # inside the 裝備評分 tab; the league workflow gets its own tab.
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.grid(row=1, column=0, padx=12, pady=(0, 4), sticky="nsew")
        tab_gear = self.tabs.add("裝備評分")
        tab_league = self.tabs.add("聯賽評分")
        tab_gear.grid_columnconfigure(0, weight=1)
        tab_gear.grid_rowconfigure(3, weight=1)   # results frame expands
        tab_league.grid_columnconfigure(0, weight=1)
        tab_league.grid_rowconfigure(0, weight=1)

        # League tab content is fully self-contained in LeaguePanel.
        from .league_panel import LeaguePanel
        self.league_panel = LeaguePanel(tab_league, self)
        self.league_panel.grid(row=0, column=0, sticky="nsew")

        # ----- gear tab: Excel row --------------------------------------
        # Same wraplength treatment as adb_label so that long workbook
        # paths + "N 名成員 / 上次更新" metadata don't clip the 變更 Excel…
        # button.
        excel_frame = ctk.CTkFrame(tab_gear)
        excel_frame.grid(row=0, column=0, padx=0, pady=(4, 6), sticky="ew")
        excel_frame.grid_columnconfigure(0, weight=1)
        self.excel_label = ctk.CTkLabel(
            excel_frame, text="Excel： -", anchor="w", justify="left", wraplength=560,
        )
        self.excel_label.grid(row=0, column=0, padx=(12, 8), pady=8, sticky="w")
        ctk.CTkButton(excel_frame, text="變更 Excel…", width=120, command=self._choose_workbook).grid(
            row=0, column=1, padx=(8, 12), pady=8, sticky="ne"
        )

        # ----- gear tab: toolbar
        # Button order per spec: 成員管理 → 🚀 開始掃描 → 中止.
        # 開始掃描 is the headline action so we paint it in a saturated
        # green (matches the GitHub "primary" look) and bold the label.
        toolbar = ctk.CTkFrame(tab_gear)
        toolbar.grid(row=1, column=0, padx=0, pady=(0, 6), sticky="ew")
        toolbar.grid_columnconfigure(0, weight=1)

        self.rename_btn = ctk.CTkButton(
            toolbar, text="成員管理", width=120, command=self._open_rename_dialog,
        )
        self.rename_btn.grid(row=0, column=1, padx=4, pady=8)

        self.start_btn = ctk.CTkButton(
            toolbar,
            text="🚀 開始掃描",
            width=180,
            height=36,
            command=self._start_scan,
            fg_color="#2ea043",        # GitHub primary green
            hover_color="#238636",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.start_btn.grid(row=0, column=2, padx=4, pady=8)

        self.cancel_btn = ctk.CTkButton(
            toolbar, text="中止", width=80, command=self._cancel_scan, state="disabled",
            fg_color="#c1351c", hover_color="#a02e18",
        )
        self.cancel_btn.grid(row=0, column=3, padx=4, pady=8)

        # ----- gear tab: progress + scan results
        self.progress_bar = ctk.CTkProgressBar(tab_gear)
        self.progress_bar.grid(row=2, column=0, padx=0, pady=(0, 0), sticky="ew")
        self.progress_bar.set(0.0)

        results_frame = ctk.CTkFrame(tab_gear)
        results_frame.grid(row=3, column=0, padx=0, pady=(6, 4), sticky="nsew")
        results_frame.grid_columnconfigure(0, weight=1)
        # row 4 (scrollable area) expands; rows 0-3 (capture, scan, title,
        # header) stay their natural height so the column titles stick.
        results_frame.grid_rowconfigure(4, weight=1)

        # Two separate status lines:
        #   row 0  capture_status — producer side ("截圖: N/M")
        #   row 1  scan_status    — consumer side ("分析截圖中… 第 N/M 頁")
        # The capture line is hidden during rescan-from-pages flows
        # (those have no producer, just OCR work on existing PNGs).
        self.capture_status_label = ctk.CTkLabel(
            results_frame, text="", anchor="w",
        )
        self.capture_status_label.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        self.capture_status_label.grid_remove()  # hidden until a live scan starts

        self.scan_status_label = ctk.CTkLabel(
            results_frame, text="等待開始掃描…", anchor="w"
        )
        self.scan_status_label.grid(row=1, column=0, padx=12, pady=(4, 0), sticky="ew")

        ctk.CTkLabel(
            results_frame, text="即時分析截圖結果", anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=2, column=0, padx=12, pady=(4, 2), sticky="w")

        # Sticky header — sits ABOVE the scrollable frame so the column
        # titles never move while data rows scroll. Column-weight 1 on
        # the name column makes it absorb all extra horizontal space, so
        # the gear / delta columns hug the right edge as the window grows.
        header = ctk.CTkFrame(results_frame, fg_color=("#dadada", "#333333"))
        header.grid(row=3, column=0, padx=12, pady=(0, 0), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        header_font = ctk.CTkFont(size=12, weight="bold")
        ctk.CTkLabel(header, text="#", width=_LIVE_IDX_W, anchor="w",
                     font=header_font).grid(row=0, column=0, padx=(8, 4), pady=6, sticky="w")
        ctk.CTkLabel(header, text="成員", anchor="w",
                     font=header_font).grid(row=0, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkLabel(header, text="裝評", width=_LIVE_GEAR_W, anchor="e",
                     font=header_font).grid(row=0, column=2, padx=4, pady=6, sticky="e")
        ctk.CTkLabel(header, text="變化", width=_LIVE_DELTA_W, anchor="e",
                     font=header_font).grid(row=0, column=3, padx=(4, 8), pady=6, sticky="e")

        self.results_list = ctk.CTkScrollableFrame(
            results_frame, label_text=None,
            fg_color=("#fafafa", "#1f1f1f"),
        )
        self.results_list.grid(row=4, column=0, padx=12, pady=(0, 8), sticky="nsew")
        # Inner-frame col 0 expands so each row frame fills the full width.
        self.results_list.grid_columnconfigure(0, weight=1)

        # ----- status bar (shared)
        self.status_bar = ctk.CTkLabel(
            self, text="", anchor="w", font=ctk.CTkFont(size=12)
        )
        self.status_bar.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="ew")

    # ============================================================== menu

    def _build_menu(self) -> None:
        """Attach the top menu bar.

        Everything non-headline lives under 工具 (per the 2026-07-03 spec):

          工具
            ├─ 重新分析裝評截圖 ▸
            │    ├─ 重新讀取 members.json…   — fast, uses cached OCR output
            │    └─ 重新分析截圖歷史 page…    — re-runs OCR on saved page PNGs
            └─ 重新分析聯賽截圖…              — re-analyse previous league
                                              capture sessions per screen
        """
        menubar = Menu(self)
        tools_menu = Menu(menubar, tearoff=0)

        rescan_menu = Menu(tools_menu, tearoff=0)
        rescan_menu.add_command(
            label="重新讀取 members.json…",
            command=self._rescan_from_members_json,
        )
        rescan_menu.add_command(
            label="重新分析截圖歷史 page…",
            command=self._rescan_reocr_pages,
        )
        tools_menu.add_cascade(label="重新分析裝評截圖", menu=rescan_menu)
        tools_menu.add_command(
            label="重新分析聯賽截圖…",
            command=self._open_league_reanalyze_dialog,
        )
        tools_menu.add_separator()
        tools_menu.add_command(
            label="從雲端名冊同步…",
            command=self._start_roster_sync,
        )
        tools_menu.add_command(
            label="清除 Google 授權",
            command=self._forget_google_auth,
        )
        menubar.add_cascade(label="工具", menu=tools_menu)
        self._rescan_menu = rescan_menu
        self._tools_menu = tools_menu
        self.config(menu=menubar)

    def _open_league_reanalyze_dialog(self) -> None:
        """Re-analyse previously captured league pages (no recapture)."""
        # Jump to the league tab so the per-screen progress is visible.
        self.tabs.set("聯賽評分")
        self.league_panel.open_reanalyze_dialog()

    # ------------------------------------------------- roster sync (menu)

    def _forget_google_auth(self) -> None:
        from ..roster_sync.google_sheets import clear_token
        clear_token()
        messagebox.showinfo(
            "已清除 Google 授權",
            "下次執行「從雲端名冊同步」時會重新開啟瀏覽器授權，\n"
            "屆時可改用其他 Google 帳號。",
        )

    def _start_roster_sync(self) -> None:
        """工具 → 從雲端名冊同步…

        Fetch + diff run on a worker thread — the first run blocks on the
        browser consent for up to minutes, and even routine runs do two
        HTTPS round-trips. Only the review dialog touches the Tk thread."""
        if getattr(self, "_roster_sync_busy", False):
            messagebox.showinfo("同步進行中", "雲端名冊同步已在進行中。")
            return
        self._roster_sync_busy = True
        self._tools_menu.entryconfig("從雲端名冊同步…", state="disabled")

        # Pick up a freshly pasted sheet_url without an app restart.
        reload_config()

        def _worker() -> None:
            from ..roster_sync.diff import WorkbookLoadError, compute_plan
            from ..roster_sync.google_sheets import (
                SheetAccessError,
                fetch_sheet_grid,
            )
            from ..roster_sync.sheet_parse import (
                SheetParseError,
                parse_roster_grid,
            )
            try:
                grid = fetch_sheet_grid()
                plan = compute_plan(parse_roster_grid(grid))
            except (SheetAccessError, SheetParseError, WorkbookLoadError) as exc:
                self.after(0, self._roster_sync_failed, str(exc))
            except Exception as exc:  # noqa: BLE001 — surface, don't die silently
                logger.exception("roster sync failed")
                self.after(0, self._roster_sync_failed, f"未預期的錯誤：{exc}")
            else:
                self.after(0, self._roster_sync_ready, plan)

        threading.Thread(target=_worker, daemon=True, name="roster-sync").start()

    def _roster_sync_done(self) -> None:
        self._roster_sync_busy = False
        self._tools_menu.entryconfig("從雲端名冊同步…", state="normal")

    def _roster_sync_failed(self, message: str) -> None:
        self._roster_sync_done()
        messagebox.showerror("雲端名冊同步失敗", message)

    def _roster_sync_ready(self, plan) -> None:
        self._roster_sync_done()
        if not plan.changes and not plan.peak_updates:
            body = (
                f"試算表成員 {plan.sheet_member_count} 人，"
                "兩份 Excel 已與試算表一致，沒有需要同步的變更。"
            )
            if plan.sheet_stale_peaks:
                body += (
                    f"\n\n⚠ 有 {plan.sheet_stale_peaks} 筆成員的本地最高裝評"
                    "高於表單上的裝備評分（或表單空白）——"
                    "記得將掃描完的裝備評分更新至雲端名冊！"
                )
            if plan.warnings:
                body += "\n\n注意：\n" + "\n".join(f"⚠ {w}" for w in plan.warnings)
            messagebox.showinfo("雲端名冊同步", body)
            return
        from .roster_sync_dialog import RosterSyncDialog
        RosterSyncDialog(self, plan)

    # ============================================================== env

    # =================================================== env detection (async)
    #
    # Anything that talks to ldconsole or ADB has to live on a worker
    # thread — both shell out to subprocesses, and the round-trip can
    # take 1–3 seconds depending on instance count. Blocking the Tk main
    # thread while that runs would freeze the whole window (including
    # the "重新偵測" button itself, which is exactly what the user just
    # complained about). The pattern below is:
    #
    #   1. Main thread flips the button into a busy state immediately so
    #      the user sees the click took effect.
    #   2. Worker thread does all subprocess I/O and posts the result
    #      back via `self.after(0, ...)`.
    #   3. Main thread updates labels / dropdown and re-enables the
    #      button when the result arrives.

    def _refresh_env_status(self) -> None:
        if self._detecting:
            return
        # Drop the config cache so any edits to config.ini between scans
        # are picked up without restarting the app. Cheap (just a tiny
        # INI file) and the user expects "重新偵測" to redo everything.
        cfg = reload_config()
        # Parse failures silently reset EVERY setting to defaults — that
        # must never be invisible. Warn once per breakage (the flag
        # re-arms after the user fixes the file and re-detects).
        if cfg.load_error:
            if not getattr(self, "_config_error_warned", False):
                self._config_error_warned = True
                messagebox.showwarning(
                    "config.ini 解析失敗",
                    "config.ini 無法解析，所有設定已改用內建預設值：\n\n"
                    f"{cfg.load_error}\n\n"
                    "請修正檔案內容（常見原因：同一區段內出現重複的設定名稱），"
                    "存檔後再按「重新偵測」。",
                )
        else:
            self._config_error_warned = False
        self._detecting = True
        self.detect_btn.configure(state="disabled", text="偵測中…")
        # Immediate hints so the user knows the click registered.
        self.adb_label.configure(text="ADB： ⏳ 偵測中…", text_color=COLOR_NEUTRAL_FG)
        self.device_label.configure(
            text="模擬器： ⏳ 偵測中… (列舉多開器、檢查 ADB 偵錯狀態)",
            text_color=COLOR_NEUTRAL_FG,
        )
        self.instance_hint.configure(text="", text_color=COLOR_NEUTRAL_FG)
        self.status_bar.configure(text="正在偵測雷電多開器與 ADB 狀態…")
        threading.Thread(
            target=self._env_probe_worker, name="env-probe", daemon=True
        ).start()

    def _env_probe_worker(self) -> None:
        """All subprocess work happens here, off the Tk thread."""
        adb_path = adb_binary()
        console_path = ldconsole_binary()
        adb_error: str | None = None
        adb_version: str | None = None
        client: AdbClient | None = None
        instances: list[InstanceProbeResult] = []

        if adb_path is None:
            adb_error = "找不到 adb.exe (LDPlayer 安裝目錄)"
        else:
            try:
                client = AdbClient(binary=adb_path)
                adb_version = client.version()
            except AdbError as exc:
                adb_error = str(exc)
                client = None

        if client is not None and console_path is not None:
            try:
                instances = find_ldplayer_instances(console_path, client)
            except Exception as exc:  # noqa: BLE001
                logger.exception("find_ldplayer_instances failed")
                adb_error = adb_error or f"列舉多開器失敗：{exc}"

        # Post the result back to the UI thread.
        self.after(
            0,
            lambda: self._apply_env_probe(
                adb_path=adb_path,
                adb_version=adb_version,
                adb_error=adb_error,
                console_path=console_path,
                client=client,
                instances=instances,
            ),
        )

    def _apply_env_probe(
        self,
        *,
        adb_path: Path | None,
        adb_version: str | None,
        adb_error: str | None,
        console_path: Path | None,
        client: AdbClient | None,
        instances: list[InstanceProbeResult],
    ) -> None:
        """Apply probe results to widgets. Runs on the Tk main thread."""
        self._detecting = False
        self.detect_btn.configure(state="normal", text="重新偵測")

        self.adb_path = adb_path
        self.ldconsole_path = console_path
        self.adb_client = client

        if adb_error or adb_path is None:
            self.adb_label.configure(
                text=f"ADB： ❌ {adb_error or '找不到 adb.exe'}", text_color="#cc3333",
            )
            # Surface the manual picker so the user can point us at
            # their LDPlayer install without editing config.ini by hand.
            self.browse_ld_btn.grid()
        else:
            extra = "ldconsole ✅" if console_path else "ldconsole ❌ 找不到"
            # Path on one line, version + ldconsole status on the next so
            # the wraplength=560 label doesn't push the 重新偵測 button
            # off the window edge.
            self.adb_label.configure(
                text=f"ADB： ✅ {adb_path}\n     ({adb_version}) ｜ {extra}",
                text_color=COLOR_NEUTRAL_FG,
            )
            # adb found, but ldconsole might still be missing — keep
            # offering the manual picker in that case so the user can
            # supply a complete LDPlayer install in one click.
            if console_path is None:
                self.browse_ld_btn.grid()
            else:
                self.browse_ld_btn.grid_remove()

        self._populate_instance_menu(instances)

        # Status bar summary so the user knows the detection actually ran.
        n_online = sum(1 for r in instances if r.status == InstanceStatus.ONLINE)
        n_adb_off = sum(1 for r in instances if r.status == InstanceStatus.ADB_OFF)
        n_off = sum(1 for r in instances if r.status == InstanceStatus.NOT_RUNNING)
        if not instances:
            self.status_bar.configure(text="偵測完成 — 未列舉到任何 LDPlayer 多開器。")
        else:
            self.status_bar.configure(
                text=(
                    f"偵測完成 — 共 {len(instances)} 個多開器："
                    f"✅ {n_online} 可用 ｜ ⚠ {n_adb_off} ADB 尚未啟用 ｜ ❌ {n_off} 未啟動"
                )
            )

    # ----------------------------------------------------- instance picker

    @staticmethod
    def _format_instance_label(r: InstanceProbeResult) -> str:
        """Human-friendly dropdown label for one probed LDPlayer instance."""
        icon = {
            InstanceStatus.ONLINE: "✅",
            InstanceStatus.ADB_OFF: "⚠",
            InstanceStatus.NOT_RUNNING: "❌",
            InstanceStatus.OFFLINE: "⚠",
        }.get(r.status, "?")
        suffix = {
            InstanceStatus.ONLINE: "已連線",
            InstanceStatus.ADB_OFF: "ADB 尚未啟用",
            InstanceStatus.NOT_RUNNING: "未啟動",
            InstanceStatus.OFFLINE: "離線",
        }.get(r.status, r.status.value)
        return f"{icon} [{r.instance.index}] {r.instance.name}  ({suffix})"

    def _populate_instance_menu(self, instances: list[InstanceProbeResult]) -> None:
        """Sync the dropdown widget against the latest probe result."""
        self.instances = list(instances)
        if not instances:
            self.instance_menu.configure(values=["(未偵測到多開器)"], state="disabled")
            self.instance_var.set("(未偵測到多開器)")
            self.instance_hint.configure(
                text=(
                    "找不到任何 LDPlayer 多開器。\n"
                    "1. 確認 LDMultiplayer 已開啟並至少有一個實例。\n"
                    "2. 若 ldconsole.exe 不在 LDPlayer 安裝路徑，請手動指定。"
                ),
                text_color="#cc6600",
            )
            self.device_label.configure(
                text="模擬器： ❌ 未偵測到", text_color="#cc3333",
            )
            self.device_serial = None
            return

        labels = [self._format_instance_label(r) for r in instances]
        self.instance_menu.configure(values=labels, state="normal")

        # Default selection priority: previously-picked serial (if still
        # online) → first ONLINE instance → first instance overall.
        target_index: int | None = None
        if self.device_serial:
            for i, r in enumerate(instances):
                if r.serial == self.device_serial:
                    target_index = i
                    break
        if target_index is None:
            for i, r in enumerate(instances):
                if r.status == InstanceStatus.ONLINE:
                    target_index = i
                    break
        if target_index is None:
            target_index = 0

        self.instance_var.set(labels[target_index])
        self._apply_instance_selection(instances[target_index])

    def _on_instance_selected(self, label: str) -> None:
        """User picked a different LDPlayer from the dropdown."""
        for r in self.instances:
            if self._format_instance_label(r) == label:
                self._apply_instance_selection(r)
                return

    def _apply_instance_selection(self, r: InstanceProbeResult) -> None:
        """Refresh device label + hint when the selected instance changes."""
        if r.status == InstanceStatus.ONLINE and r.serial:
            if self.adb_client is not None:
                self.adb_client.default_serial = r.serial
            self.device_serial = r.serial
            self.device_label.configure(
                text=f"模擬器： ✅ {r.serial}  ({r.instance.name})",
                text_color=COLOR_NEUTRAL_FG,
            )
            self.instance_hint.configure(text=r.detail, text_color="#1d8a3d")
        else:
            self.device_serial = None
            self.device_label.configure(
                text=f"模擬器： {r.instance.name}  狀態 = {r.status.value}",
                text_color="#cc6600",
            )
            self.instance_hint.configure(text=r.detail, text_color="#cc6600")

    # ============================================================== workbook

    def _load_workbook(self, path: Path, *, silent: bool = False) -> None:
        try:
            wb = GuildScoresWorkbook.load(path)
        except Exception as exc:  # noqa: BLE001
            self.workbook = None
            self.excel_label.configure(text=f"Excel： ❌ 無法讀取 {path}: {exc}", text_color="#cc3333")
            if not silent:
                messagebox.showerror("讀取失敗", str(exc))
            return
        self.workbook = wb
        self.workbook_path = path
        filled = sum(1 for r in wb.records if r.correct_nickname)
        last_update = wb.capture_days[-1] if wb.capture_days else "尚無紀錄"
        suffix = "(新檔，尚未建立)" if not path.exists() else ""
        # Two-line layout — path on top, member count + last-update on the
        # next line — so the 變更 Excel… button on the right stays
        # visible even with long sync-drive paths.
        meta_line = f"{filled} 名成員 ｜ 上次更新: {last_update}"
        if suffix:
            meta_line = f"{meta_line}  {suffix}"
        # Duplicate 遊戲ID makes exact matching first-wins-only — warn at
        # load time (mirrors the league roster's issue surfacing).
        from ..storage.excel import duplicate_nickname_issues
        issues = duplicate_nickname_issues(wb.records)
        if issues:
            preview = "；".join(issues[:2])
            more = f"…等共 {len(issues)} 項" if len(issues) > 2 else ""
            self.excel_label.configure(
                text=f"Excel： ⚠ {path}\n     {meta_line}\n"
                     f"     請先修正：{preview}{more}",
                text_color="#cc6600",
            )
            return
        self.excel_label.configure(
            text=f"Excel： ✅ {path}\n     {meta_line}",
            text_color=COLOR_NEUTRAL_FG,
        )

    def _browse_ldplayer_dir(self) -> None:
        """Let the user manually pick the LDPlayer install folder.

        Shown next to 重新偵測 when auto-detection couldn't find adb.exe
        (or only adb but not ldconsole.exe). The picked path is
        validated against the binaries we actually need, then persisted
        to config.ini's ``[paths] ldplayer_dir =`` so the choice
        survives an app restart. A successful pick automatically
        re-runs env detection so the user sees the green checkmark.
        """
        from ..utils.config import set_path_value, reload_config
        from ..utils.paths import config_path

        # Seed the picker with whatever's currently configured (or the
        # first existing entry in the auto-detect list) so the user
        # opens close to where LDPlayer usually lives.
        from ..utils.paths import known_ldplayer_dirs
        from ..utils.config import app_config
        cfg = app_config()
        initial = ""
        if cfg.ldplayer_dir and cfg.ldplayer_dir.is_dir():
            initial = str(cfg.ldplayer_dir)
        else:
            for d in known_ldplayer_dirs():
                if d.is_dir():
                    initial = str(d)
                    break

        chosen = filedialog.askdirectory(
            title="選擇 LDPlayer 安裝資料夾（裡面要有 adb.exe 與 ldconsole.exe）",
            initialdir=initial,
        )
        if not chosen:
            return

        picked = Path(chosen)
        adb_ok = (picked / "adb.exe").is_file()
        ldconsole_ok = (picked / "ldconsole.exe").is_file()
        if not adb_ok:
            messagebox.showerror(
                "資料夾不正確",
                (
                    f"在這個資料夾找不到 adb.exe：\n\n    {picked}\n\n"
                    "請選擇 LDPlayer 主程式目錄（通常裡面同時有 adb.exe 與 ldconsole.exe）。"
                ),
            )
            return
        if not ldconsole_ok:
            # adb is the hard requirement; ldconsole is nice-to-have for
            # instance enumeration. Warn but continue so the user can at
            # least scan if their LDPlayer install is partial.
            if not messagebox.askyesno(
                "找不到 ldconsole.exe",
                (
                    f"adb.exe 找得到，但 ldconsole.exe 不在這個資料夾：\n\n    {picked}\n\n"
                    "沒有 ldconsole 的話，自動列出多開實例的功能會不能用。\n"
                    "仍要使用這個資料夾嗎？"
                ),
            ):
                return

        # Persist + reload + re-probe. set_path_value preserves comments
        # in config.ini so the user's other edits aren't trampled.
        try:
            set_path_value(config_path(), "ldplayer_dir", str(picked))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("寫入設定檔失敗", f"無法更新 config.ini：{exc}")
            return
        reload_config()
        self._refresh_env_status()

    def _choose_workbook(self) -> None:
        from ..utils.config import set_path_value

        initial = self.workbook_path.parent if self.workbook_path else user_data_dir()
        chosen = filedialog.askopenfilename(
            title="選擇 guild_scores.xlsx",
            initialdir=str(initial),
            filetypes=[("Excel Workbook", "*.xlsx")],
        )
        if not chosen:
            return
        picked = Path(chosen)
        self._load_workbook(picked)
        # Persist the user's pick so next launch reopens the same workbook.
        # Only save when load succeeded — a broken file shouldn't pin itself.
        if self.workbook is not None:
            try:
                set_path_value(config_path(), "workbook_path", str(picked))
                reload_config()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to persist workbook_path to config.ini")
                messagebox.showwarning(
                    "寫入設定檔失敗",
                    f"已載入工作簿，但無法更新 config.ini：{exc}",
                )

    def _open_rename_dialog(self) -> None:
        if self.workbook is None or not self.workbook.records:
            messagebox.showinfo(
                "尚未載入成員",
                "請先載入或建立一個含成員資料的 guild_scores.xlsx。",
            )
            return
        dialog = RenameDialog(
            self,
            workbook=self.workbook,
            on_renamed=self._on_player_renamed,
        )
        dialog.focus_set()

    def _on_player_renamed(self, record_index: int, new_name: str) -> None:
        # Persist immediately so the rename survives an app crash. Save
        # takes a backup automatically.
        assert self.workbook is not None
        try:
            backup = self.workbook.save(backup=True)
        except PermissionError:
            messagebox.showerror(
                "儲存失敗",
                "Excel 似乎正被開啟中，請先關閉再試一次。剛剛的改名動作會保留在記憶體中。",
            )
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("儲存失敗", str(exc))
            return
        self._load_workbook(self.workbook_path, silent=True)
        msg = f"已將第 {record_index + 1} 列改名為「{new_name}」"
        if backup:
            msg += f"，原檔備份於 {backup.name}"
        self.status_bar.configure(text=msg)

    # ============================================================== scan

    def _start_scan(self) -> None:
        if self.runner and self.runner.is_running():
            return
        if self.adb_client is None or self.device_serial is None:
            messagebox.showerror(
                "環境未就緒", "請先確認 ADB 與雷電模擬器都已連上 (按「重新偵測」)。"
            )
            return
        if self.workbook is None:
            messagebox.showerror(
                "Excel 未載入",
                "請先選擇一個 guild_scores.xlsx (或讓系統用預設路徑建立新檔)。",
            )
            return

        # Pre-scan checklist confirmation. Doing this BEFORE the OCR
        # warm-up so the user isn't sitting through 10s of loading just
        # to discover they need to switch LDPlayer screens.
        if not messagebox.askyesno(
            "開始前確認",
            (
                "請確認以下兩件事：\n\n"
                "  1. LDPlayer 已切到「公會 → 公會成員」頁面\n"
                "  2. 成員列表已滑到最上方\n\n"
                "按「是」立即開始截圖；按「否」回到主畫面繼續準備。"
            ),
            parent=self,
        ):
            return

        # Lazy-load OCR engines (5–15s the first time). Show a banner.
        if self.ocr_primary is None and not self.ocr_loading:
            self.ocr_loading = True
            self.status_bar.configure(text="正在載入 OCR 模型（首次約 10 秒）…")
            threading.Thread(
                target=self._load_ocr_then_scan, name="ocr-warmup", daemon=True
            ).start()
            return

        if self.ocr_loading:
            # User clicked while warm-up still in progress; the warm-up
            # thread will fire _begin_scan when ready.
            return

        self._begin_scan()

    def _load_ocr_then_scan(self) -> None:
        """Background OCR warm-up; schedules the scan once loaded.

        Honours the ``[ocr]`` section of config.ini so the user can pick
        a heavier primary model (v5-server) for higher accuracy at the
        cost of scan time.
        """
        cfg = app_config()
        primary_quality = cfg.ocr_primary_model or "v5-mobile"
        fallback_quality = cfg.ocr_fallback_model or "v5-server"
        try:
            primary = OcrEngine(model_quality=primary_quality)
            fallback: OcrEngine | None
            if fallback_quality.lower() in ("off", "none", "disabled"):
                fallback = None
            else:
                fallback = OcrEngine(model_quality=fallback_quality)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR warm-up failed")
            self.after(0, lambda exc=exc: self._on_ocr_failed(exc))
            return
        logger.info(
            "OCR engines loaded: primary={} fallback={}",
            primary_quality, fallback_quality,
        )
        self.after(0, lambda: self._on_ocr_ready(primary, fallback))

    def _on_ocr_ready(self, primary: OcrEngine, fallback: OcrEngine | None) -> None:
        self.ocr_primary = primary
        self.ocr_fallback = fallback
        self.ocr_loading = False
        self.status_bar.configure(text="OCR 載入完成，開始掃描…")
        # If the caller queued a scan while warm-up was running, run it now.
        callback = self._ocr_ready_callback
        self._ocr_ready_callback = None
        if callback is not None:
            callback()
        else:
            self._begin_scan()

    def _on_ocr_failed(self, exc: BaseException) -> None:
        self.ocr_loading = False
        self._ocr_ready_callback = None
        self.status_bar.configure(text=f"OCR 載入失敗：{exc}")
        messagebox.showerror("OCR 載入失敗", str(exc))

    def _begin_scan(self) -> None:
        assert self.workbook is not None
        assert self.adb_client is not None
        assert self.ocr_primary is not None

        # Re-read the workbook from disk every time the user kicks off
        # a fresh scan — the user may have hand-edited Excel between
        # scans (rename, ID changes, peak override). Without this we'd
        # be matching captures against a stale in-memory snapshot.
        self._load_workbook(self.workbook_path, silent=True)
        if self.workbook is None:
            messagebox.showerror(
                "Excel 載入失敗", "重新載入工作簿時出錯，請檢查檔案是否被其他程式佔用。",
            )
            return

        # Reset live list and progress.
        self._reset_live_table()
        # Stamp start time for the elapsed-time readout on the review dialog.
        import time as _t
        self._scan_start_perf = _t.perf_counter()
        self.progress_bar.set(0.0)
        self.scan_status_label.configure(text="分析截圖已啟動，請勿操作雷電視窗…")
        # Show the capture-progress line for live scans (producer side).
        self.capture_status_label.configure(
            text="📸 截圖中: 0 張 (準備中…)", text_color=COLOR_NEUTRAL_FG,
        )
        self.capture_status_label.grid()

        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.rename_btn.configure(state="disabled")
        self._set_rescan_menu_state("disabled")

        self.runner = ScanRunner(
            adb=self.adb_client,
            ocr=self.ocr_primary,
            fallback_ocr=self.ocr_fallback,
            records=self.workbook.records,
            capture_day=date.today().isoformat(),
        )
        self.runner.start()
        self.after(self.POLL_INTERVAL_MS, self._poll_runner_events)

    def _cancel_scan(self) -> None:
        """User-initiated abort. Routes to live-scan or rescan path."""
        # Live scan path — ScanRunner owns the cancel + session-folder cleanup.
        if self.runner and self.runner.is_running():
            if not messagebox.askyesno(
                "確認中止分析截圖",
                (
                    "停止後將會：\n"
                    "  • 停止後續截圖與 OCR 解析\n"
                    "  • 刪除本次在 data/captures/ 建立的暫存資料夾\n\n"
                    "確定要中止嗎？"
                ),
                parent=self,
            ):
                return
            # The askyesno above is modal but Tk keeps pumping events —
            # the scan may have FINISHED while the dialog was open (the
            # summary/review flow already took over). Cancelling now
            # would only flip a stale flag and leave the status label
            # lying, so just bail.
            if not (self.runner and self.runner.is_running()):
                return
            self.runner.cancel()
            self.scan_status_label.configure(text="正在中止分析截圖…")
            return

        # Rescan path — worker polls self._reocr_cancel between pages.
        # No screenshot to delete because we're re-OCRing existing PNGs.
        if not self._reocr_cancel and self._reocr_worker_running():
            if not messagebox.askyesno(
                "確認中止重新分析截圖",
                (
                    "停止後將會：\n"
                    "  • 停止後續頁面的 OCR 解析\n"
                    "  • 已 OCR 的列保留顯示，但不會寫入工作簿\n"
                    "  • 原始 page_*.png 不會被刪除\n\n"
                    "確定要中止嗎？"
                ),
                parent=self,
            ):
                return
            self._reocr_cancel = True
            self.scan_status_label.configure(text="正在中止重新分析截圖…")
            return

    def _reocr_worker_running(self) -> bool:
        """True if a reocr-worker thread is currently alive."""
        for t in threading.enumerate():
            if t.name == "reocr-worker" and t.is_alive():
                return True
        return False

    def _poll_runner_events(self) -> None:
        if self.runner is None:
            return
        drained = 0
        while drained < 200:
            try:
                event = self.runner.events.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._handle_runner_event(event)

        if self.runner.is_running():
            self.after(self.POLL_INTERVAL_MS, self._poll_runner_events)
        else:
            # Drain anything left that arrived between the last get and now.
            while True:
                try:
                    event = self.runner.events.get_nowait()
                except queue.Empty:
                    break
                self._handle_runner_event(event)

    def _handle_runner_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "status":
            self.scan_status_label.configure(text=event[1])
        elif kind == "progress":
            _page, total, _new = event[1], event[2], event[3]
            page_idx = event[1]
            max_pages = self.runner.max_pages if self.runner else 60
            self.progress_bar.set(min(1.0, (page_idx + 1) / max_pages))
            self.scan_status_label.configure(
                text=(
                    f"分析截圖中… 第 {page_idx + 1} / {max_pages} 頁"
                    f" ｜ 累計 {total} 位成員"
                )
            )
        elif kind == "capture_progress":
            captured, total = event[1], event[2]
            self._handle_capture_progress(captured, total)
        elif kind in ("member_pending", "member_resolved"):
            self._upsert_live_member(event[1])
        elif kind == "not_member_list":
            self._handle_not_member_list(event[1])
        elif kind == "summary":
            self._handle_summary(event[1])
        elif kind == "error":
            self._finish_scan_with_error(event[1])

    def _handle_capture_progress(self, captured: int, total: int) -> None:
        """Producer-side progress: "截圖: N / M".

        When the producer reports it has captured all ``total`` frames,
        switch the label to a "可自由操控模擬器" success message —
        the producer already disconnected ADB at that point so the user
        can interact with LDPlayer while the consumer finishes OCR.
        """
        if captured >= total:
            self.capture_status_label.configure(
                text=(
                    f"✅ 截圖已完成 ({captured} / {total}) — "
                    "可自由操控模擬器，OCR 分析會在背景持續執行"
                ),
                text_color=COLOR_UP,
            )
        else:
            self.capture_status_label.configure(
                text=f"📸 截圖中: {captured} / {total} 張",
                text_color=COLOR_NEUTRAL_FG,
            )

    # ============================================================== live table
    #
    # The table is rendered with a STICKY header (a regular CTkFrame above
    # the scrollable area) so the column titles stay put while rows scroll.
    # Each row is keyed by ``dedup_key`` so a row that was first emitted
    # as "pending" can be repainted in place when the post-scan fuzzy
    # phase resolves it — no duplicate rows.
    #
    # Cell widths are the module-level _LIVE_*_W constants so the header
    # and every data row line up regardless of name length or gear digits.

    def _reset_live_table(self) -> None:
        for child in list(self.results_list.winfo_children()):
            child.destroy()
        self._row_widgets.clear()
        self._row_order.clear()
        self._live_members.clear()

    def _upsert_live_member(self, m: LiveMember) -> None:
        """Add a new row or update an existing one for ``m.dedup_key``.

        Rows are kept sorted by ``player_id`` (None goes to the bottom)
        per spec, so a pending row that later resolves with ID = 42
        will slide into its proper position next to the other ID-40s.
        """
        widgets = self._row_widgets.get(m.dedup_key)
        is_new = widgets is None
        if widgets is None:
            widgets = self._add_live_row(0)
            self._row_widgets[m.dedup_key] = widgets
        self._live_members[m.dedup_key] = m
        # Re-sort + re-grid every row in the table whenever a member
        # arrives or updates. With ~150 rows this is fast enough; Tk
        # handles 1k+ grid_configure calls per frame comfortably.
        self._reorder_live_table()
        self._paint_live_row(widgets, m)
        # Auto-scroll to bottom only when a new pending/unmatched row
        # (no ID) appeared — those land at the bottom anyway, so the
        # autoscroll keeps the latest activity visible. Resolved rows
        # may have inserted higher up, so we don't scroll for those.
        if is_new and m.player_id is None:
            try:
                self.results_list._parent_canvas.yview_moveto(1.0)  # noqa: SLF001
            except AttributeError:
                pass

    def _reorder_live_table(self) -> None:
        """Place every row at its sort-by-ID position. Unmatched go last."""
        def _sort_key(k: str) -> tuple:
            lm = self._live_members[k]
            # First sort bucket: 0 = has ID (matched/fuzzy), 1 = no ID.
            # Second: the ID itself, ascending. Tie-break on dedup_key
            # so two unmatched rows have a stable order between updates.
            if lm.player_id is None:
                return (1, 0, k)
            return (0, lm.player_id, k)

        sorted_keys = sorted(self._live_members.keys(), key=_sort_key)
        for new_idx, dedup_key in enumerate(sorted_keys):
            row = self._row_widgets[dedup_key]["row"]
            row.grid_configure(row=new_idx)
        self._row_order = sorted_keys

    def _add_live_row(self, row_idx: int) -> dict:
        """Create the widgets for one row; returns a handles dict.

        Same column-weight pattern as the header: # and name on the left,
        gear and delta hugging the right edge via ``sticky="e"`` plus
        ``columnconfigure(1, weight=1)`` on the row frame.
        """
        row = ctk.CTkFrame(self.results_list, fg_color=COLOR_EXACT_BG)
        row.grid(row=row_idx, column=0, padx=2, pady=1, sticky="ew")
        row.grid_columnconfigure(1, weight=1)
        # Placeholder — populated by _paint_live_row with the matched
        # row's player_id (or "—" if still pending / unmatched).
        idx_cell = ctk.CTkLabel(
            row, text="—", width=_LIVE_IDX_W, anchor="w",
        )
        idx_cell.grid(row=0, column=0, padx=(8, 4), pady=4, sticky="w")
        name_cell = ctk.CTkLabel(row, text="", anchor="w")
        name_cell.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        gear_cell = ctk.CTkLabel(
            row, text="", width=_LIVE_GEAR_W, anchor="e",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        gear_cell.grid(row=0, column=2, padx=4, pady=4, sticky="e")
        delta_cell = ctk.CTkLabel(row, text="", width=_LIVE_DELTA_W, anchor="e")
        delta_cell.grid(row=0, column=3, padx=(4, 8), pady=4, sticky="e")
        return {
            "row": row,
            "idx": idx_cell,
            "name": name_cell,
            "gear": gear_cell,
            "delta": delta_cell,
        }

    def _paint_live_row(self, w: dict, m: LiveMember) -> None:
        """Refresh row contents + colour for the latest LiveMember state."""
        # Name column — display rules per spec:
        #   exact / fuzzy_review → matched_to (correct_nickname)
        #   pending → "(辨識中…)"  while waiting for the post-scan fuzzy pass
        #   unmatched → "未配對" + small OCR hint
        if m.decision == "pending":
            name_text = "(辨識中…)"
        elif m.decision == "unmatched":
            name_text = f"未配對  ｜  OCR: {m.ocr_nickname}"
        else:
            name_text = m.matched_to or m.ocr_nickname

        # "#" column shows Excel column-A ID for matched rows; pending
        # and unmatched rows have no ID yet so we show an em-dash. This
        # is the user-facing identifier — keeps the live table and the
        # workbook visually consistent.
        idx_text = f"#{m.player_id}" if m.player_id is not None else "—"
        w["idx"].configure(text=idx_text)

        # Gear and delta strictly separate per spec — when there's no
        # previous value we leave the delta cell blank rather than
        # echoing the gear number into it.
        gear_text = f"{m.gear_score:,}" if m.gear_score else "—"
        delta_text = ""
        delta_color = COLOR_NEUTRAL_FG
        if m.delta is not None:
            if m.delta > 0:
                delta_text, delta_color = f"(+{m.delta:,})", COLOR_UP
            elif m.delta < 0:
                delta_text, delta_color = f"({m.delta:,})", COLOR_DOWN
            else:
                delta_text, delta_color = "(±0)", COLOR_NEUTRAL_FG

        # Background tint matches state. Fuzzy + unmatched both wash red
        # so the user immediately knows "verify this one".
        if m.decision in ("fuzzy_review", "unmatched"):
            row_bg = COLOR_REVIEW_BG
        elif m.decision == "pending":
            row_bg = COLOR_PENDING_BG
        else:
            row_bg = COLOR_EXACT_BG

        w["row"].configure(fg_color=row_bg)
        w["name"].configure(text=name_text)
        w["gear"].configure(text=gear_text)
        w["delta"].configure(text=delta_text, text_color=delta_color)

    def _finish_scan_with_error(self, exc: BaseException) -> None:
        self.scan_status_label.configure(text=f"分析截圖失敗：{exc}")
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.rename_btn.configure(state="normal")
        self._set_rescan_menu_state("normal")
        messagebox.showerror("分析截圖失敗", str(exc))

    # ============================================================ post-scan
    #
    # The new flow per spec:
    #   1. ScanRunner emits "summary" with the CaptureResult + per-member
    #      live data + missed + unmatched lists.
    #   2. We carry the captured-member dicts over (matches members.json
    #      shape) so the workbook can re-merge after the user reviews.
    #   3. ReviewDialog opens for the user to optionally fill missed
    #      gear scores. OCR-only entries are info-only.
    #   4. On confirm, we run merge_capture with append_unmatched=False
    #      and mark_missed=False — fuzzy matches still land as red rows,
    #      but truly unmatched OCR rows are never written and missed
    #      rows are only touched for ones the user typed a value for.

    # ------------------------------------------------------------ timing

    def _consume_scan_elapsed_seconds(self) -> float | None:
        """Return seconds since _begin_scan / _rescan_* started, or None.

        Clears the timestamp on read so subsequent re-renders of the
        summary text don't keep prepending the same number.
        """
        if self._scan_start_perf is None:
            return None
        import time as _t
        elapsed = _t.perf_counter() - self._scan_start_perf
        self._scan_start_perf = None
        return elapsed

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """123.4s → '2 分 03 秒'; 12.3s → '12.3 秒'."""
        if seconds < 60:
            return f"{seconds:.1f} 秒"
        m, s = divmod(int(seconds), 60)
        return f"{m} 分 {s:02d} 秒"

    def _handle_summary(self, summary: SummaryPayload) -> None:
        result = summary.result

        # Cancellation path — purge the partial session and bail without
        # touching the workbook or showing the review dialog.
        if self.runner is not None and self.runner.was_cancelled:
            self._cleanup_after_cancel()
            return

        # Total elapsed time from when the user clicked 開始掃描 to now.
        # Reported on the status label so the user has a sense of how
        # long the run took (截圖 + 分析 combined).
        elapsed_s = self._consume_scan_elapsed_seconds()
        elapsed_text = self._format_elapsed(elapsed_s) if elapsed_s else ""

        if result is not None:
            self.scan_status_label.configure(
                text=(
                    f"分析截圖完成 — 共分析截圖出 {len(result.members)} 位，"
                    f"{len(result.pages)} 頁"
                    + (f"，總耗時 {elapsed_text}" if elapsed_text else "")
                    + f" (halt: {result.halt_reason})"
                )
            )
            captured = [self._captured_member_to_dict(cm) for cm in result.members]
            # Heads-up if the early-stop heuristic fired suspiciously
            # soon — usually means the scroll glitched or the OCR
            # connected several "no-new-rows" misreads in a row.
            # 8 pages = ~40 members; below that on a 150-person guild
            # is unexpected and worth flagging before the user commits.
            if (
                result.halt_reason
                in ("no_new_rows", "scroll_stuck_at_end", "list_frozen")
                and len(result.pages) < 8
            ):
                messagebox.showwarning(
                    "成員過少 — 提前結束截圖",
                    (
                        f"截圖只跑到第 {len(result.pages)} 頁就判定到底了，"
                        f"共辨識到 {len(result.members)} 位成員。\n\n"
                        "可能原因：\n"
                        "  • 公會實際成員數確實較少\n"
                        "  • LDPlayer 滑動未順利、提前判定到底\n"
                        "  • OCR 連續多頁誤判為「無新成員」\n\n"
                        "請在統整視窗中確認結果是否符合預期，"
                        "若不滿意可按取消放棄本次結果，重新從成員列表最上方再跑一次。"
                    ),
                    parent=self,
                )
        else:
            self.scan_status_label.configure(
                text=(
                    f"重新分析截圖完成 — 共分析截圖出 {len(summary.members)} 位"
                    + (f"，總耗時 {elapsed_text}" if elapsed_text else "")
                )
            )
            # For rescan-from-session the caller stuffed the source dicts
            # into _pending_captures already.
            captured = self._pending_captures

        self.progress_bar.set(1.0)
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.rename_btn.configure(state="normal")
        self._set_rescan_menu_state("normal")

        self._pending_captures = captured
        self._pending_capture_day = summary.capture_day

        # Always open the review dialog at end-of-scan per spec. Even
        # when both lists are empty the user gets a confirm-and-save tap.
        ReviewDialog(
            self,
            capture_day=summary.capture_day,
            missed=summary.missed,
            unmatched=summary.unmatched,
            on_confirm=self._on_review_confirmed,
            workbook=self.workbook,
            on_renamed=self._on_inline_rename_from_review,
        )

    @staticmethod
    def _captured_member_to_dict(cm: CapturedMember) -> dict:
        """Serialise a CapturedMember in the members.json shape."""
        return {
            "dedup_key": cm.dedup_key,
            "nickname": cm.nickname,
            "nickname_confidence": cm.nickname_confidence,
            "gear_score": cm.gear_score,
            "gear_score_confidence": cm.gear_score_confidence,
            "candidates": [asdict(c) for c in cm.candidates],
            "first_seen_page": cm.first_seen_page,
            "first_seen_y": cm.first_seen_y,
            "sightings": cm.sightings,
        }

    def _on_review_confirmed(self, decisions) -> bool:  # type: ignore[no-untyped-def]
        """ReviewDialog callback — commits the user's choices.

        Returns ``True`` on successful write (dialog will close);
        ``False`` if the write failed (ReviewDialog stays open so the
        user can fix the underlying issue — typically Excel still
        having the file open — and click 確認並儲存 again).

        ``decisions.cancelled=True`` means the user cancelled / ✕ in the
        review dialog. Per spec, we skip EVERY write in that case —
        not even phase-1/2 exact matches get persisted, and the
        SaveSuccessDialog (with the blessing line) does NOT pop up.
        Workbook on disk stays exactly as it was before the scan.

        Otherwise commits:
          * ``manual_gear`` — typed gear for pure-missed rows or fuzzy
            overrides where the user decided NOT to trust the proposal
          * ``approved_fuzzy`` — phase-3 candidates the user ticked 套用
            on; gear + OCR nickname both write through.
          * ``assigned_unmatched`` — ❷-section captures manually
            assigned to a member via the dropdown; same write path as
            ``approved_fuzzy``.
        """
        if self.workbook is None or self._pending_capture_day is None:
            return True

        if decisions.cancelled:
            self.status_bar.configure(
                text="已取消本次分析 — workbook 未變動",
            )
            self._pending_captures = []
            self._pending_capture_day = None
            return True

        capture_label = self._pending_capture_day
        captured = self._pending_captures

        try:
            # Phase 1+2 exact matches apply through merge_capture. Phase
            # 3 fuzzy hits are collected on merge_result.fuzzy_candidates
            # but NOT applied (the user's approvals drive that below).
            # Phase 5 unmatched aren't appended either (we never auto-
            # append in the GUI flow).
            merge_result = self.workbook.merge_capture(
                captured, capture_label,
                append_unmatched=False,
                mark_missed=False,
            )
            # Apply the user's per-row choices.
            counts = self.workbook.apply_review_decisions(
                capture_label,
                manual_gear=decisions.manual_gear,
                approved_fuzzy=decisions.approved_fuzzy,
                assigned_unmatched=decisions.assigned_unmatched,
            )
            backup = self.workbook.save(backup=True)
        except PermissionError:
            messagebox.showerror(
                "Excel 被開啟中",
                (
                    "Excel 對 guild_scores.xlsx 仍持有寫入鎖。\n"
                    "請先關閉 Excel（或 OneDrive 之類同步軟體）後，\n"
                    "再回到統整視窗按一次「確認並儲存」即可。"
                ),
                parent=self,
            )
            # IMPORTANT: keep review dialog open so the user doesn't
            # lose their manual gear inputs / fuzzy approvals.
            return False
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("寫入失敗", str(exc), parent=self)
            return False

        self._load_workbook(self.workbook_path, silent=True)
        bits = [
            f"已寫入 {self.workbook_path.name}",
            f"配對成功 {len(merge_result.updated)}",
            f"模糊提案 {len(merge_result.fuzzy_candidates)} (已套用 {counts['fuzzy']})",
            f"手填補登 {counts['manual']}",
            f"共遺漏 {len(merge_result.missed_in_capture)}",
            (
                f"未配對 OCR {len(merge_result.unmatched_captured)} "
                f"(指認 {counts['assigned']}、其餘未寫入)"
            ),
        ]
        if backup:
            bits.append(f"備份 {backup.name}")
        self.status_bar.configure(text=" ｜ ".join(bits))
        # Show a styled success toast — workbook-written confirmation +
        # the requested blessing line ("祝您 精煉都會上，打怪掉紅裝！").
        SaveSuccessDialog(self, workbook_path=str(self.workbook_path))
        # Clear pending payload so a stray re-confirm can't double-write.
        self._pending_captures = []
        self._pending_capture_day = None
        return True

    def _on_inline_rename_from_review(self, record_index: int, new_name: str) -> None:
        """ReviewDialog's per-row 改名 button fires this.

        The rename is already applied in-memory by RenameSubDialog; we
        save the workbook immediately so the change survives even if
        the user later cancels the rest of the review.
        """
        if self.workbook is None:
            return
        try:
            self.workbook.save(backup=True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("改名儲存失敗", str(exc), parent=self)
            return
        self.status_bar.configure(
            text=f"已將成員 (record #{record_index + 1}) 改名為「{new_name}」並寫入 Excel"
        )

    def _handle_not_member_list(self, info: dict) -> None:
        """First-page sanity check failed — guide the user to retry.

        ScanRunner has already set ``_cancelled`` and signalled the
        producer to stop; we just surface a clear message and let the
        usual cancellation cleanup remove the empty session folder when
        the summary event lands.
        """
        n_rows = info.get("n_rows", 0)
        n_valid = info.get("n_valid_gear", 0)
        self.scan_status_label.configure(
            text=(
                f"未偵測到公會成員列表 — 第一頁解析到 {n_rows} 列、"
                f"其中 {n_valid} 列有合理的裝評數字。"
            ),
        )
        messagebox.showwarning(
            "未偵測到公會成員列表",
            (
                f"第一張截圖只解析出 {n_rows} 列、其中 {n_valid} 列有合理的裝評數字。\n\n"
                "可能原因：\n"
                "  • LDPlayer 當下不在 公會 → 公會成員 頁面\n"
                "  • 成員列表沒滑到最上方\n"
                "  • 解析度設定不符（建議 1280×720 或 1920×1080）\n\n"
                "請切到正確頁面後重新按「開始掃描」。"
            ),
            parent=self,
        )

    def _cleanup_after_cancel(self) -> None:
        """Wipe the partial capture folder after the user aborts a scan."""
        import shutil
        session_dir = self.runner.session_dir if self.runner else None
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.rename_btn.configure(state="normal")
        self._set_rescan_menu_state("normal")
        self.progress_bar.set(0.0)
        if session_dir is not None:
            try:
                shutil.rmtree(session_dir, ignore_errors=True)
                self.scan_status_label.configure(
                    text=f"已中止分析截圖並刪除 {session_dir.name}"
                )
                self.status_bar.configure(text=f"已刪除暫存資料夾 {session_dir}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("failed to delete cancelled session dir")
                self.scan_status_label.configure(
                    text=f"分析截圖已中止，但無法刪除 {session_dir}: {exc}"
                )
        else:
            self.scan_status_label.configure(text="已中止分析截圖")

    def _set_rescan_menu_state(self, state: str) -> None:
        """Enable / disable both 重新分析截圖 menu items together.

        Called from the scan lifecycle (disabled while a scan runs, re-
        enabled when it finishes). No-op if the menu hasn't been built
        yet (e.g. during the very first frame before _build_ui runs).
        """
        menu = getattr(self, "_rescan_menu", None)
        if menu is None:
            return
        try:
            for i in range(menu.index("end") + 1):
                menu.entryconfig(i, state=state)
        except Exception:
            pass

    # ============================================================ rescan

    def _rescan_from_members_json(self) -> None:
        """Re-run matching against an existing capture session folder.

        No ADB/OCR involved — we just load the cached ``members.json``
        and pipe it through the same matching + review pipeline as a
        live scan. Useful when you've added new ``correct_nickname``
        rows since the original scan and want to retry the auto-match
        without re-screenshotting or re-OCRing.
        """
        if self.workbook is None:
            messagebox.showinfo("尚未載入 Excel", "請先載入 guild_scores.xlsx。")
            return
        initial = user_data_dir() / "captures"
        if not initial.is_dir():
            initial = user_data_dir()
        # Pick the json file directly (per spec) — easier than asking
        # the user to drill into the right folder. We derive the
        # session_dir from the chosen file's parent so the thumbnail
        # lookup (pages/page_NNN.png) still works.
        chosen = filedialog.askopenfilename(
            initialdir=str(initial),
            title="選擇要重新讀取的 members.json",
            filetypes=[("members.json", "members*.json"), ("JSON", "*.json"), ("所有檔案", "*.*")],
        )
        if not chosen:
            return
        members_json = Path(chosen)
        if not members_json.is_file():
            messagebox.showerror("檔案不存在", f"{members_json}")
            return
        session_dir = members_json.parent

        # Re-read the workbook fresh — same reason as the live scan,
        # the user may have hand-edited Excel between scans.
        self._load_workbook(self.workbook_path, silent=True)
        # Stamp scan start for the elapsed-time readout on the review dialog.
        import time as _t
        self._scan_start_perf = _t.perf_counter()

        # session_dir name "YYYYMMDD_HHMMSS" → date string the workbook
        # uses as the column header. Falls back to today only if the
        # folder name doesn't look like our convention.
        name = session_dir.name
        if len(name) >= 8 and name[:8].isdigit():
            capture_day = f"{name[0:4]}-{name[4:6]}-{name[6:8]}"
        else:
            capture_day = date.today().isoformat()

        try:
            captured_dicts = json.loads(members_json.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("讀取失敗", f"{members_json}\n\n{exc}")
            return

        # Rebuild CapturedMember objects just enough for the matching
        # helpers — they only touch nickname, gear, page_index, dedup_key.
        captured_members: list[CapturedMember] = []
        for m in captured_dicts:
            captured_members.append(CapturedMember(
                dedup_key=m.get("dedup_key") or str(m.get("gear_score") or "0"),
                nickname=m.get("nickname"),
                nickname_confidence=m.get("nickname_confidence"),
                gear_score=int(m.get("gear_score") or 0),
                gear_score_confidence=float(m.get("gear_score_confidence") or 0.0),
                candidates=[],
                first_seen_page=int(m.get("first_seen_page") or 0),
                first_seen_y=int(m.get("first_seen_y") or 0),
                sightings=int(m.get("sightings") or 1),
            ))

        # Rescan-from-members.json has no producer / capture phase; hide
        # the "截圖: N/50" line so it doesn't show stale text.
        self.capture_status_label.grid_remove()

        live_members, missed, unmatched = resolve_captures(
            captured_members, self.workbook.records, capture_day,
            pages_dir=session_dir / "pages",
        )

        # Repaint the live table so the rescan shows the same per-row
        # detail (correct name, diff, red rows) the live flow does.
        self._reset_live_table()
        for lm in live_members:
            self._upsert_live_member(lm)

        # Stash captures + day before opening the review dialog so the
        # confirm callback can replay through merge_capture.
        self._pending_captures = captured_dicts
        self._pending_capture_day = capture_day

        self.scan_status_label.configure(
            text=(
                f"重新分析截圖完成（{session_dir.name}） — "
                f"共分析截圖出 {len(live_members)} 位 ｜ 共遺漏 {len(missed)} ｜ 未配對 {len(unmatched)}"
            ),
        )

        ReviewDialog(
            self,
            capture_day=capture_day,
            missed=missed,
            unmatched=unmatched,
            on_confirm=self._on_review_confirmed,
            workbook=self.workbook,
            on_renamed=self._on_inline_rename_from_review,
        )

    def _rescan_reocr_pages(self) -> None:
        """Re-OCR the page_*.png files in a session and rebuild members.

        Heavy operation (~10s per page on v5-mobile + v5-server fallback,
        so ~7 min for a 40-page guild). Runs entirely in a worker thread
        so the UI keeps painting progress updates.

        Flow:
          1. User picks a session folder under data/captures/.
          2. Lazy-load OCR engines if not already loaded — when this
             trips, we cache the folder args in ``self._reocr_pending_args``
             so the post-warm-up retry doesn't ask the user to pick again.
          3. Worker thread iterates each PNG, runs parse_member_page +
             refine_nicknames, then dedups into CapturedMember list.
          4. On finish, jumps back to the main thread to repaint the
             live table and open the review dialog — same as live scan.
        """
        if self.workbook is None:
            messagebox.showinfo("尚未載入 Excel", "請先載入 guild_scores.xlsx。")
            return
        if self.runner and self.runner.is_running():
            messagebox.showinfo("已有分析截圖進行中", "請先等分析截圖完成或中止。")
            return

        # If we're being re-entered after the OCR warm-up callback,
        # skip the folder picker — the args are already stashed.
        if self._reocr_pending_args is not None:
            session_dir, page_files, capture_day = self._reocr_pending_args
            self._reocr_pending_args = None
        else:
            initial = user_data_dir() / "captures"
            if not initial.is_dir():
                initial = user_data_dir()
            folder = filedialog.askdirectory(
                initialdir=str(initial),
                title="選擇要重新跑 OCR 的擷取資料夾",
            )
            if not folder:
                return
            session_dir = Path(folder)
            pages_dir = session_dir / "pages"
            if not pages_dir.is_dir():
                messagebox.showerror(
                    "無效的擷取資料夾",
                    f"{session_dir} 內找不到 pages/ — 請選擇 data/captures/ 下的子資料夾。",
                )
                return
            page_files = sorted(pages_dir.glob("page_*.png"))
            if not page_files:
                messagebox.showerror(
                    "找不到圖片",
                    f"{pages_dir} 內沒有 page_*.png — 無法重新分析截圖。",
                )
                return

            # Folder name → capture day (same convention as live scan).
            name = session_dir.name
            if len(name) >= 8 and name[:8].isdigit():
                capture_day = f"{name[0:4]}-{name[4:6]}-{name[6:8]}"
            else:
                capture_day = date.today().isoformat()

        # OCR engines are required. Stash args + kick off warm-up; the
        # warm-up callback re-enters this method with _reocr_pending_args
        # set, so the folder picker is bypassed on the retry.
        if self.ocr_primary is None:
            self._reocr_pending_args = (session_dir, page_files, capture_day)
            self._ocr_ready_callback = self._rescan_reocr_pages
            if not self.ocr_loading:
                self.ocr_loading = True
                self.status_bar.configure(
                    text=f"正在載入 OCR 模型（首次約 10 秒）— 載入完成後會自動對 {len(page_files)} 張圖片開跑…"
                )
                threading.Thread(
                    target=self._load_ocr_then_scan, name="ocr-warmup", daemon=True,
                ).start()
            # Else: warm-up already running; callback set, will retry.
            return

        # OCR ready — kick off the worker.
        logger.info(
            "rescan re-OCR start: session={} pages={} capture_day={}",
            session_dir.name, len(page_files), capture_day,
        )
        # Re-read workbook fresh — user may have hand-edited Excel
        # since the previous load (see "reload before analyse" spec).
        self._load_workbook(self.workbook_path, silent=True)
        self._reset_live_table()
        self._reocr_cancel = False
        # Stamp start time for elapsed-time readout on the review dialog.
        import time as _t
        self._scan_start_perf = _t.perf_counter()
        self.progress_bar.set(0.0)
        # Re-OCR has no producer phase — hide the camera-progress line.
        self.capture_status_label.grid_remove()
        self.scan_status_label.configure(
            text=f"重新分析截圖歷史 page：{session_dir.name} ｜ 共 {len(page_files)} 張圖片"
        )
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self._set_rescan_menu_state("disabled")
        self.rename_btn.configure(state="disabled")
        self.status_bar.configure(
            text=f"正在對 {len(page_files)} 張歷史截圖跑 OCR（約 {len(page_files) * 10}s）…"
        )

        threading.Thread(
            target=self._reocr_worker,
            name="reocr-worker",
            args=(session_dir, page_files, capture_day),
            daemon=True,
        ).start()

    def _reocr_worker(
        self, session_dir: Path, page_files: list[Path], capture_day: str,
    ) -> None:
        """Background OCR pass over an existing session's page PNGs.

        Mirrors the live-scan flow exactly:
          * Phase A — for every newly-deduped member, try exact match
            against ``correct_nickname`` / ``latest_ocr_nickname`` and
            post a LiveMember to the UI immediately so the user sees
            rows pop in as each page is OCR'd.
          * Phase B — after the last page, fuzzy-resolve anything still
            pending. Each fuzzy resolution also posts to the live table
            (the row repaints red on the way to the review dialog).

        Per-page cancel check (``self._reocr_cancel``) lets the 中止
        button bail out cleanly between pages without leaving a half-
        OCR'd state.
        """
        import cv2
        import numpy as np
        from ..matching import build_matcher
        from ..vision.layout import DEFAULT_LAYOUT
        from ..vision.parser import parse_member_page, refine_nicknames
        from .review_dialog import UnmatchedReviewItem
        from .scan_runner import (
            LiveMember, _build_missed, _resolve_exact, _resolve_fuzzy,
        )

        try:
            assert self.ocr_primary is not None
            assert self.workbook is not None
            layout = DEFAULT_LAYOUT
            records = self.workbook.records
            matcher = build_matcher(records)
            members_dict: dict[str, CapturedMember] = {}
            seen_keys: set[str] = set()
            pending: dict[str, CapturedMember] = {}
            # Track phase-A claims separately so _build_missed can tell
            # exact-matched (already applied) from phase-3 candidates
            # (need user confirmation in the review dialog).
            exact_matched_indices: set[int] = set()

            for page_idx, png_path in enumerate(page_files):
                if self._reocr_cancel:
                    self.after(0, self._on_reocr_cancelled)
                    return

                image = cv2.imdecode(
                    np.fromfile(str(png_path), dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if image is None:
                    continue
                rows = parse_member_page(image, self.ocr_primary, layout)
                if self.ocr_fallback is not None:
                    # Honour config.ini's fallback_threshold so the
                    # rescan worker matches live-scan accuracy. Falls
                    # back to 0.90 (CaptureSession's default) if
                    # unconfigured.
                    cfg = app_config()
                    threshold = (
                        cfg.ocr_fallback_threshold
                        if cfg.ocr_fallback_threshold is not None
                        else 0.90
                    )
                    refine_nicknames(
                        image, rows, self.ocr_fallback, layout,
                        minimum_confidence=threshold,
                    )
                # Edge-row filter (keep empty-nickname rows for review).
                # Asymmetric margins matching CaptureSession._filter_complete:
                # top 1% (lenient — first row anchors close to y_top),
                # bottom 4% (strict — partial-render phantoms cluster here).
                H = image.shape[0]
                y_top, y_bottom = layout.rows_y_pixels(H)
                top_margin = int(H * 0.01)
                bottom_margin = int(H * 0.04)
                kept = [
                    r for r in rows
                    if y_top + top_margin <= r.row_y <= y_bottom - bottom_margin
                ]

                # Diff dedup keys before/after so we can phase-A-match
                # only the NEW members rather than re-matching everyone
                # on every page.
                pre_keys = set(members_dict.keys())
                self._merge_reocr_rows(kept, members_dict, page_idx)
                new_keys = set(members_dict.keys()) - pre_keys

                # Phase A — per-member exact match; emit LiveMember now.
                for key in new_keys:
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    cm = members_dict[key]
                    lm = _resolve_exact(cm, matcher, records, capture_day)
                    if lm is None:
                        pending[key] = cm
                        placeholder = LiveMember(
                            ocr_nickname=(cm.nickname or "").strip() or "(空)",
                            gear_score=cm.gear_score,
                            confidence=cm.nickname_confidence,
                            matched_to=None,
                            previous_gear=None,
                            delta=None,
                            decision="pending",
                            record_index=None,
                            page_index=cm.first_seen_page,
                            dedup_key=cm.dedup_key,
                            row_y=cm.first_seen_y,
                            player_id=None,
                        )
                        self.after(
                            0, lambda m=placeholder: self._upsert_live_member(m),
                        )
                    else:
                        exact_matched_indices.add(lm.record_index) if lm.record_index is not None else None
                        self.after(0, lambda m=lm: self._upsert_live_member(m))

                # Page-level progress (count + bar).
                self.after(
                    0,
                    lambda i=page_idx, total=len(page_files), n=len(members_dict):
                        self._on_reocr_progress(i, total, n),
                )

            if self._reocr_cancel:
                self.after(0, self._on_reocr_cancelled)
                return

            # Phase B — fuzzy-resolve every still-pending row. Each
            # resolution repaints the placeholder row in the live table.
            pages_dir = session_dir / "pages"
            unmatched_items: list[UnmatchedReviewItem] = []
            fuzzy_hits: dict[int, LiveMember] = {}
            for key, cm in pending.items():
                if self._reocr_cancel:
                    self.after(0, self._on_reocr_cancelled)
                    return
                lm = _resolve_fuzzy(cm, matcher, records, capture_day)
                self.after(0, lambda m=lm: self._upsert_live_member(m))
                if lm.decision == "fuzzy_review" and lm.record_index is not None:
                    fuzzy_hits[lm.record_index] = lm
                elif lm.decision == "unmatched":
                    unmatched_items.append(UnmatchedReviewItem(
                        ocr_nickname=lm.ocr_nickname,
                        gear_score=lm.gear_score,
                        confidence=lm.confidence,
                        page_index=lm.page_index,
                        image_path=(
                            pages_dir / f"page_{lm.page_index:03d}.png"
                            if lm.page_index is not None else None
                        ),
                        row_y=lm.row_y,
                    ))

            missed_items = _build_missed(
                matcher, records,
                exact_matched=exact_matched_indices,
                fuzzy_hits=fuzzy_hits,
            )

            captured_dicts = [
                self._captured_to_members_json_dict(cm)
                for cm in members_dict.values()
            ]
            # Persist the new members.json alongside the original so the
            # user can A/B compare without losing anything.
            (session_dir / "members_reocr.json").write_text(
                json.dumps(captured_dicts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            self.after(
                0,
                lambda: self._on_reocr_done(
                    session_dir, capture_day, captured_dicts,
                    missed_items, unmatched_items,
                ),
            )
        except BaseException as exc:  # noqa: BLE001
            logger.exception("re-OCR worker crashed")
            self.after(0, lambda exc=exc: self._on_reocr_failed(exc))

    @staticmethod
    def _merge_reocr_rows(
        rows, members: dict, page_idx: int,
    ) -> None:
        """Same dedup logic as CaptureSession._merge, minus the queue."""
        from ..capture.session import _dedup_key
        for row in rows:
            key = _dedup_key(row)
            if key in members:
                members[key].sightings += 1
                if (
                    row.nickname is not None
                    and row.nickname_confidence is not None
                    and (
                        members[key].nickname_confidence is None
                        or row.nickname_confidence > members[key].nickname_confidence
                    )
                ):
                    members[key].nickname = row.nickname
                    members[key].nickname_confidence = row.nickname_confidence
                continue
            members[key] = CapturedMember(
                dedup_key=key,
                nickname=row.nickname,
                nickname_confidence=row.nickname_confidence,
                gear_score=row.gear_score,
                gear_score_confidence=row.gear_score_confidence,
                candidates=list(row.candidates),
                first_seen_page=page_idx,
                first_seen_y=row.row_y,
            )

    @staticmethod
    def _captured_to_members_json_dict(cm: CapturedMember) -> dict:
        return {
            "dedup_key": cm.dedup_key,
            "nickname": cm.nickname,
            "nickname_confidence": cm.nickname_confidence,
            "gear_score": cm.gear_score,
            "gear_score_confidence": cm.gear_score_confidence,
            "candidates": [asdict(c) for c in cm.candidates],
            "first_seen_page": cm.first_seen_page,
            "first_seen_y": cm.first_seen_y,
            "sightings": cm.sightings,
        }

    def _on_reocr_progress(self, page_idx: int, total: int, n_unique: int) -> None:
        self.progress_bar.set((page_idx + 1) / total)
        self.scan_status_label.configure(
            text=f"重新分析截圖歷史 page：第 {page_idx + 1} / {total} 張 ｜ 累計 {n_unique} 位"
        )

    def _on_reocr_done(
        self,
        session_dir: Path,
        capture_day: str,
        captured_dicts: list[dict],
        missed: list,
        unmatched: list,
    ) -> None:
        """Worker finished cleanly — the live table is already populated
        with phase-A + phase-B updates posted from the worker thread.
        We just unfreeze the toolbar, stash captured payload for the
        review confirm callback, and open the review dialog.
        """
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._set_rescan_menu_state("normal")
        self.rename_btn.configure(state="normal")
        self.progress_bar.set(1.0)

        self._pending_captures = captured_dicts
        self._pending_capture_day = capture_day

        n_resolved = len(self._live_members)
        self.scan_status_label.configure(
            text=(
                f"重新分析截圖歷史 page 完成（{session_dir.name}） — "
                f"共分析截圖出 {n_resolved} 位 ｜ 共遺漏 {len(missed)} ｜ 未配對 {len(unmatched)}"
            ),
        )
        logger.info(
            "rescan re-OCR done: session={} resolved={} missed={} unmatched={}",
            session_dir.name, n_resolved, len(missed), len(unmatched),
        )
        ReviewDialog(
            self,
            capture_day=capture_day,
            missed=missed,
            unmatched=unmatched,
            on_confirm=self._on_review_confirmed,
            workbook=self.workbook,
            on_renamed=self._on_inline_rename_from_review,
        )

    def _on_reocr_cancelled(self) -> None:
        """User pressed 中止 mid-rescan. Reset the toolbar; the partial
        live table is left in place so the user can see what was OCR'd
        so far, but nothing gets written to the workbook."""
        self._reocr_cancel = False
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._set_rescan_menu_state("normal")
        self.rename_btn.configure(state="normal")
        self.scan_status_label.configure(text="已中止重新分析截圖歷史 page")
        self.status_bar.configure(text="（已 OCR 的列保留顯示，未寫入工作簿）")

    def _on_reocr_failed(self, exc: BaseException) -> None:
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._set_rescan_menu_state("normal")
        self.rename_btn.configure(state="normal")
        self.scan_status_label.configure(text=f"重新分析截圖歷史 page 失敗：{exc}")
        messagebox.showerror("重新分析截圖歷史 page 失敗", str(exc))

    # ============================================================== lifecycle

    def on_close(self) -> None:
        # League capture/analysis still running? Closing now silently
        # drops any recognised-but-unwritten scans (pages stay on disk,
        # but re-analysis costs a fresh round of Gemini calls) — make
        # the user say yes explicitly.
        league = getattr(self, "league_panel", None)
        if league is not None and (
            league.runner.capturing or league.runner.analyzing
        ):
            if not messagebox.askyesno(
                "聯賽處理中",
                "聯賽拍攝／分析仍在進行中，現在關閉會捨棄尚未產出的辨識結果。\n"
                "（截圖已存檔，之後可用「工具 → 重新分析聯賽截圖」重跑，"
                "但需要重新呼叫 Gemini。）\n\n確定要關閉嗎？",
            ):
                return
        if self.runner and self.runner.is_running():
            self.runner.cancel()
        try:
            if self.adb_client is not None:
                self.adb_client.disconnect()
        except Exception:
            pass
        # disconnect() only drops the device link — the adb.exe server
        # process spawned by start-server stays parked in the background
        # until kill-server is invoked, which surprises users who close
        # the GUI and then see adb.exe still in Task Manager. Issue an
        # explicit kill-server so the bundled exe leaves the machine
        # clean on shutdown.
        try:
            if self.adb_client is not None:
                self.adb_client.kill_server()
        except Exception:
            pass
        self.destroy()


def run() -> int:
    app = RoGearSyncApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run())
