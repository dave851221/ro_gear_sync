"""聯賽評分 tab — five screen cards, background analysis, snapshot write-out.

Workflow (per the 2026-07-02 spec):

  1. The user switches the game to one of the five screens (主戰場×輸出/輔助,
     副戰場×輸出/輔助/戰略) and clicks that card's 拍攝 button.
  2. Capture is fast (~1.5 s/page); when it finishes the card announces
     「✅ 拍攝完成，可切換下一個畫面」 — recognition continues in the
     background with its progress painted on the card.
  3. Any time at least one screen is analysed, 產出結果 becomes available —
     partial battles are fine. It runs roster matching, pops the review
     dialog, then writes league_scores_YYYYMMDD_HHMM.xlsx and updates the
     roster's Last_OCR_ID column.
"""
from __future__ import annotations

import queue
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from ..league import (
    load_roster,
    merge_battle,
    roster_issues,
    update_roster_ocr,
    write_battle,
)
from ..utils.logging import logger
from ..utils.paths import league_roster_path
from .league_review_dialog import LeagueReviewDialog, apply_review_decisions
from .league_runner import SCREEN_DEFS, LeagueRunner, screen_key, screen_label

_POLL_MS = 80


class LeaguePanel(ctk.CTkFrame):
    """Content of the 聯賽評分 tab. ``app`` is the RoGearSyncApp instance —
    we read its live ADB selection at capture time and reuse its status bar."""

    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.runner = LeagueRunner()
        # screen_key → card widget dict.
        self._cards: dict[str, dict] = {}
        self._writing = False
        # Pending after() id for the busy-state recheck (see
        # _schedule_busy_recheck) — one in flight at a time.
        self._busy_recheck_id: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self._build()
        self.after(_POLL_MS, self._poll_events)

    # ------------------------------------------------------------- layout

    def _build(self) -> None:
        # ---- roster row -------------------------------------------------
        roster_frame = ctk.CTkFrame(self)
        roster_frame.grid(row=0, column=0, padx=0, pady=(4, 6), sticky="ew")
        roster_frame.grid_columnconfigure(0, weight=1)
        self.roster_label = ctk.CTkLabel(
            roster_frame, text="名冊： -", anchor="w", justify="left",
            wraplength=560,
        )
        self.roster_label.grid(row=0, column=0, padx=12, pady=8, sticky="w")
        ctk.CTkButton(
            roster_frame, text="重新讀取名冊", width=120,
            command=self._refresh_roster,
        ).grid(row=0, column=1, padx=12, pady=8, sticky="e")

        # ---- screen cards ----------------------------------------------
        cards = ctk.CTkFrame(self)
        cards.grid(row=1, column=0, padx=0, pady=(0, 6), sticky="ew")
        cards.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            cards, text="拍攝畫面（請先在遊戲切到該畫面，再按對應的拍攝）",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=12, pady=(10, 4), sticky="w")

        # Two shades of green so 主/副 battlefield buttons read at a glance:
        # 主戰場 = bright green (matches the app's primary action colour),
        # 副戰場 = deeper sea-green.
        _BF_COLOURS = {
            "main": {"fg_color": "#2ea043", "hover_color": "#238636"},
            "sub": {"fg_color": "#1a7f64", "hover_color": "#14654f"},
        }
        for i, (bf, view) in enumerate(SCREEN_DEFS, start=1):
            key = screen_key(bf, view)
            btn = ctk.CTkButton(
                cards, text=f"📸 {screen_label(bf, view)}", width=170,
                command=lambda b=bf, v=view: self._start_capture(b, v),
                **_BF_COLOURS[bf],
            )
            btn.grid(row=i, column=0, padx=(12, 8), pady=3, sticky="w")
            status = ctk.CTkLabel(cards, text="尚未拍攝", anchor="w")
            status.grid(row=i, column=1, padx=4, pady=3, sticky="ew")
            self._cards[key] = {"btn": btn, "status": status, "state": "idle"}
        ctk.CTkLabel(cards, text="").grid(row=len(SCREEN_DEFS) + 1, column=0, pady=(0, 2))

        # ---- write-out row ----------------------------------------------
        out_frame = ctk.CTkFrame(self)
        out_frame.grid(row=2, column=0, padx=0, pady=(0, 6), sticky="ew")
        out_frame.grid_columnconfigure(0, weight=1)
        self.summary_label = ctk.CTkLabel(
            out_frame, text="尚無分析完成的畫面", anchor="w", justify="left",
        )
        self.summary_label.grid(row=0, column=0, padx=12, pady=8, sticky="w")
        # Default CTk blue — matches the app's other secondary buttons;
        # green stays reserved for the capture buttons above.
        self.write_btn = ctk.CTkButton(
            out_frame, text="🧮 產出結果（寫入 Excel）", width=200, height=36,
            state="disabled",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._write_out,
        )
        self.write_btn.grid(row=0, column=1, padx=12, pady=8, sticky="e")

        self._refresh_roster()

    # ------------------------------------------------------------- roster

    def _refresh_roster(self) -> None:
        path = league_roster_path()
        try:
            roster = load_roster(path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("league roster load failed")
            self.roster_label.configure(
                text=f"名冊： ❌ 讀取失敗 {path}\n{exc}", text_color="#cc3333",
            )
            return
        if not roster:
            self.roster_label.configure(
                text=f"名冊： ⚠ {path}\n（空的或不存在——請準備 league_scores.xlsx）",
                text_color="#cc6600",
            )
            return
        n_real = sum(1 for r in roster if r.correct_nickname.strip())
        n_blank = len(roster) - n_real
        blank_txt = f"＋{n_blank} 個空位" if n_blank else ""
        # Surface roster problems (duplicate IDs, member rows without an
        # ID) the moment the file is read — the write-back keys on the ID
        # column, so these rows would silently miss their Last_OCR_ID
        # update otherwise.
        issues = roster_issues(roster)
        if issues:
            preview = "；".join(issues[:3])
            more = f"…等共 {len(issues)} 項" if len(issues) > 3 else ""
            self.roster_label.configure(
                text=(
                    f"名冊： ⚠ {path}（{n_real} 名成員{blank_txt}）\n"
                    f"名冊有問題，請先更新：{preview}{more}"
                ),
                text_color="#cc6600",
            )
            return
        self.roster_label.configure(
            text=f"名冊： ✅ {path}（{n_real} 名成員{blank_txt}）",
            text_color=("gray10", "gray90"),
        )

    # ------------------------------------------------------------ capture

    def _start_capture(self, battlefield, view) -> None:
        key = screen_key(battlefield, view)

        # Analysis failed earlier (e.g. Gemini 503 storm)? The pages are
        # still on disk — offer a re-analysis before falling back to a
        # full recapture.
        if self._cards[key]["state"] == "analyze_failed":
            if messagebox.askyesno(
                "重新分析？",
                f"「{screen_label(battlefield, view)}」上次分析失敗，"
                "但截圖都還在。\n\n是＝直接重新分析（不重拍）\n否＝整個重新拍攝",
                parent=self,
            ):
                if self.runner.retry_analysis(key):
                    self._cards[key]["state"] = "analyzing"
                    self._set_status(key, "🔎 重新分析中…", "#9a6700")
                    return
            # fall through → recapture

        client = self.app.adb_client
        serial = self.app.device_serial
        if client is None or serial is None:
            messagebox.showwarning(
                "尚未連線", "請先在上方環境檢查選擇一個已連線的模擬器實例。",
                parent=self,
            )
            return
        # Fail fast on a missing Gemini key — much kinder than letting the
        # user capture 30 pages and only then erroring in the analysis.
        try:
            from ..league.recognizer import resolve_api_key
            from ..utils.config import app_config
            resolve_api_key(app_config().gemini_api_key)
        except Exception as exc:  # noqa: BLE001 — RecognizerError with 中文 hint
            messagebox.showerror("Gemini API 金鑰未設定", str(exc), parent=self)
            return
        if self.runner.capturing:
            return  # buttons are disabled anyway
        if self.runner.scans.get(key) is not None:
            if not messagebox.askyesno(
                "重新拍攝？",
                f"「{screen_label(battlefield, view)}」已經有分析完成的資料，"
                "重新拍攝會覆蓋。確定嗎？",
                parent=self,
            ):
                return
            # Don't drop the existing scan yet — the user can still back
            # out at the final confirmation below, and cancelling there
            # must not lose an already-analysed result.
        # Final confirmation: the wrong screen wastes a full capture AND a
        # batch of Gemini calls, so make the user attest they've switched.
        if not messagebox.askyesno(
            "開始拍攝",
            f"即將拍攝「{screen_label(battlefield, view)}」。\n\n"
            "請確認遊戲畫面【已經切換】到該頁面，按「是」開始滑動與截圖。",
            parent=self,
        ):
            return
        client.default_serial = serial
        if not self.runner.start_capture(client, battlefield, view):
            return
        # The recapture is actually underway — only now discard the
        # screen's previous result.
        self.runner.scans.pop(key, None)
        self._cards[key]["state"] = "capturing"
        self._set_capture_buttons("disabled")
        self._refresh_write_state()  # write must stay disabled while busy
        self._set_status(key, "📸 拍攝中… 0 頁", "#1f6feb")
        self.app.status_bar.configure(
            text=f"聯賽拍攝中：{screen_label(battlefield, view)}（請勿操作模擬器）",
        )

    def _set_capture_buttons(self, state: str) -> None:
        for card in self._cards.values():
            card["btn"].configure(state=state)

    def _set_status(
        self, key: str, text: str,
        colour: "str | tuple[str, str] | None" = None,
    ) -> None:
        kwargs = {"text": text}
        if colour:
            kwargs["text_color"] = colour
        self._cards[key]["status"].configure(**kwargs)

    # ------------------------------------------------------------- events

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.runner.events.get_nowait()
            except queue.Empty:
                break
            # A handler bug must not eat the rest of the queue (or vanish
            # silently) — log it and keep draining.
            try:
                self._handle_event(event)
            except Exception:  # noqa: BLE001
                logger.exception("league event handling failed: {}", event[:2])
        self.after(_POLL_MS, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind, key = event[0], event[1]
        label = key  # fallback
        for bf, view in SCREEN_DEFS:
            if screen_key(bf, view) == key:
                label = screen_label(bf, view)
                break

        if kind == "capture_progress":
            self._set_status(key, f"📸 拍攝中… {event[2]} 頁", "#1f6feb")
        elif kind == "capture_done":
            n_pages, halt = event[2], event[3]
            self._cards[key]["state"] = "analyzing"
            self._set_capture_buttons("normal")
            self._set_status(
                key, f"✅ 拍攝完成（{n_pages} 頁）— 可切換下一個畫面｜等待分析…",
                "#1d8a3d",
            )
            self.app.status_bar.configure(
                text=f"「{label}」拍攝完成 — 可以切換遊戲畫面了；分析在背景進行。",
            )
            self.bell()  # audible "capture done" cue
            self._refresh_write_state()
        elif kind == "capture_error":
            self._cards[key]["state"] = "idle"
            self._set_capture_buttons("normal")
            self._set_status(key, f"❌ 拍攝失敗：{event[2]}", "#cc3333")
            self.app.status_bar.configure(text=f"「{label}」拍攝失敗。")
            self._refresh_write_state()
        elif kind == "analyze_progress":
            done, total, unique = event[2], event[3], event[4]
            # A fresh successful page clears any earlier retry warning.
            text = f"🔎 分析中… {done}/{total} 頁（已辨識 {unique} 人）"
            self._cards[key]["progress_text"] = text
            self._set_status(key, text, "#9a6700")
        elif kind == "analyze_retry":
            # Gemini is stalling (e.g. 503 high-demand) — say so after the
            # progress text so the user knows why it's slow. The next
            # analyze_progress overwrites this automatically.
            base = self._cards[key].get(
                "progress_text", "🔎 分析中…",
            )
            self._set_status(key, f"{base}｜⚠ {event[2]}", "#cc6600")
        elif kind == "analyze_done":
            scan = event[2]
            self._cards[key]["state"] = "done"
            self._cards[key].pop("progress_text", None)
            goal = scan.participant_count
            goal_txt = f"／參戰人數 {goal}" if goal is not None else ""
            self._set_status(
                key, f"✔ 完成：辨識 {len(scan.rows)} 人{goal_txt}", "#1d8a3d",
            )
            self._refresh_write_state()
        elif kind == "analyze_error":
            self._cards[key]["state"] = "analyze_failed"
            self._cards[key].pop("progress_text", None)
            self._set_status(
                key,
                f"❌ 分析失敗：{event[2]}\n"
                "　→ 截圖已保留，再按一次左側綠色按鈕即可重新分析（不用重拍）",
                "#cc3333",
            )
            self._refresh_write_state()

    def _refresh_write_state(self) -> None:
        n = len(self.runner.scans)
        if n == 0:
            self.summary_label.configure(text="尚無分析完成的畫面")
            self.write_btn.configure(state="disabled")
            return
        # Guard: never allow a write while a capture or analysis is still
        # in flight — partial-battle writes are fine, half-analysed screens
        # are not.
        busy = self.runner.capturing or self.runner.analyzing
        if busy:
            self.summary_label.configure(
                text=f"已完成 {n}/{len(SCREEN_DEFS)} 個畫面"
                     "（仍在拍攝／分析中，完成後才能產出結果）",
            )
            self.write_btn.configure(state="disabled")
            # The final analyze_done event can arrive while its worker
            # thread is still winding down, in which case ``analyzing``
            # reads True here with NO further event coming to re-enable
            # the button — recheck shortly so the state self-heals.
            self._schedule_busy_recheck()
            return
        self.summary_label.configure(
            text=f"已完成 {n}/{len(SCREEN_DEFS)} 個畫面的分析"
                 "（未掃的畫面不會出現在結果中）",
        )
        self.write_btn.configure(state="normal" if not self._writing else "disabled")

    def _schedule_busy_recheck(self) -> None:
        """Re-run :meth:`_refresh_write_state` in 500 ms, at most one
        pending at a time. Harmless while genuinely busy (the recheck is
        cheap and idempotent); essential for the last-event race where no
        further event would otherwise re-enable 產出結果."""
        if self._busy_recheck_id is not None:
            return

        def _tick() -> None:
            self._busy_recheck_id = None
            self._refresh_write_state()

        self._busy_recheck_id = self.after(500, _tick)

    # ---------------------------------------------------- re-analysis (menu)

    def open_reanalyze_dialog(self) -> None:
        """工具 → 重新分析聯賽截圖：pick previous capture sessions per screen
        and re-run recognition on them (no recapture, no game needed)."""
        if self.runner.capturing or self.runner.analyzing:
            messagebox.showwarning(
                "還在處理中", "仍有畫面在拍攝或分析中，請等它完成再重新分析。",
                parent=self,
            )
            return
        from ..utils.paths import user_data_dir
        root = user_data_dir() / "league_captures"

        none_label = "（不讀取）"
        # (bf, view) → {label: pages_dir}, newest session first.
        available: dict[tuple, dict[str, Path]] = {
            (bf, view): {} for bf, view in SCREEN_DEFS
        }
        for d in sorted(root.glob("*_*_*"), reverse=True):
            parts = d.name.split("_")
            if len(parts) < 4:
                continue
            bf, view = parts[-2], parts[-1]
            if (bf, view) not in available:
                continue
            pages = list((d / "pages").glob("*.png"))
            if not pages:
                continue
            ts = "_".join(parts[:-2])   # "20260702_204230"
            label = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}（{len(pages)} 頁）"
            available[(bf, view)][label] = d / "pages"
        if not any(available.values()):
            messagebox.showinfo(
                "沒有紀錄", f"找不到任何聯賽截圖紀錄（{root}）。", parent=self,
            )
            return

        dialog = ctk.CTkToplevel(self.winfo_toplevel())
        dialog.title("重新分析聯賽截圖")
        dialog.geometry("560x360")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        ctk.CTkLabel(
            dialog, text="選擇要重新分析的截圖紀錄（不需要開遊戲）",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=16, pady=(14, 8), sticky="w")

        choices: dict[tuple, ctk.StringVar] = {}
        for i, (bf, view) in enumerate(SCREEN_DEFS, start=1):
            sessions = available[(bf, view)]
            ctk.CTkLabel(dialog, text=screen_label(bf, view), anchor="w").grid(
                row=i, column=0, padx=(16, 8), pady=4, sticky="w",
            )
            var = ctk.StringVar(value=none_label)
            menu = ctk.CTkOptionMenu(
                dialog, variable=var, width=330,
                values=[none_label, *sessions],
                state="normal" if sessions else "disabled",
            )
            if not sessions:
                var.set("（無紀錄）")
            menu.grid(row=i, column=1, padx=(0, 16), pady=4, sticky="w")
            choices[(bf, view)] = var

        def _go() -> None:
            started = 0
            for (bf, view), var in choices.items():
                pages_dir = available[(bf, view)].get(var.get())
                if pages_dir is None:
                    continue
                if self.runner.start_analysis_from_pages(bf, view, pages_dir):
                    key = screen_key(bf, view)
                    self._cards[key]["state"] = "analyzing"
                    self._set_status(key, "🔎 重新分析中…（讀取既有截圖）", "#9a6700")
                    started += 1
            dialog.destroy()
            if started:
                self._refresh_write_state()
                self.app.status_bar.configure(
                    text=f"重新分析 {started} 個畫面的既有截圖（背景進行）。",
                )
            else:
                messagebox.showinfo("未選擇", "沒有選擇任何截圖紀錄。", parent=self)

        btns = ctk.CTkFrame(dialog, fg_color="transparent")
        btns.grid(row=len(SCREEN_DEFS) + 1, column=0, columnspan=2,
                  padx=16, pady=(12, 14), sticky="e")
        ctk.CTkButton(
            btns, text="取消", width=90, fg_color="#6e7681",
            hover_color="#57606a", command=dialog.destroy,
        ).grid(row=0, column=0, padx=4)
        ctk.CTkButton(btns, text="開始重新分析", width=140, command=_go).grid(
            row=0, column=1, padx=4,
        )

    # ----------------------------------------------------------- write out

    def _write_out(self) -> None:
        scans = list(self.runner.scans.values())
        if not scans:
            return
        # Belt-and-braces: the button is disabled while busy, but state can
        # race with a just-started capture — never write mid-flight.
        if self.runner.capturing or self.runner.analyzing:
            messagebox.showwarning(
                "還在處理中", "仍有畫面在拍攝或分析中，請等它完成再產出結果。",
                parent=self,
            )
            return
        roster_path = league_roster_path()
        try:
            roster = load_roster(roster_path)
        except Exception as exc:  # noqa: BLE001 — file locked by Excel, corrupt, …
            logger.exception("league roster load failed at write-out")
            messagebox.showerror(
                "名冊讀取失敗",
                f"讀取 {roster_path} 時發生錯誤：\n{exc}\n\n"
                "（若檔案正被 Excel 開啟，請先關閉再試一次。）",
                parent=self,
            )
            return
        if not roster:
            messagebox.showerror(
                "名冊不可用",
                f"讀不到聯賽名冊：{roster_path}\n請確認 league_scores.xlsx。",
                parent=self,
            )
            return
        issues = roster_issues(roster)
        if issues:
            preview = "\n".join(f"・{s}" for s in issues[:8])
            more = f"\n…等共 {len(issues)} 項" if len(issues) > 8 else ""
            if not messagebox.askyesno(
                "名冊有問題",
                "聯賽名冊有以下問題，建議先修正再產出\n"
                "（受影響的成員將無法回寫 Last_OCR_ID）：\n\n"
                f"{preview}{more}\n\n仍要繼續產出結果嗎？",
                parent=self,
            ):
                return
        result = merge_battle(scans, roster)

        def _confirmed(decisions: dict):
            """One write per press. The dialog stays open, so work on a
            deep copy — the displayed result must stay pristine for the
            next press with adjusted selections."""
            import copy
            applied = copy.deepcopy(result)
            apply_review_decisions(applied, decisions)
            self._writing = True
            try:
                # --- snapshot workbook ---------------------------------
                try:
                    out_path = write_battle(applied)
                except PermissionError:
                    logger.exception("league snapshot write blocked (file lock)")
                    messagebox.showerror(
                        "寫入失敗（檔案被占用）",
                        "無法寫入聯賽快照檔——輸出資料夾中的同名檔案"
                        "正被其他程式（通常是 Excel）開啟。\n"
                        "請先關閉該 Excel 檔，再按一次「確認寫入」。",
                        parent=self,
                    )
                    return None
                except Exception as exc:  # noqa: BLE001
                    logger.exception("league write-out failed")
                    messagebox.showerror("寫入失敗", str(exc), parent=self)
                    return None

                # --- roster write-back (retry loop for Excel file lock) --
                n_updated = 0
                while True:
                    try:
                        n_updated = update_roster_ocr(applied, roster_path)
                        break
                    except PermissionError:
                        logger.warning("roster write-back blocked — file locked")
                        if messagebox.askretrycancel(
                            "名冊被 Excel 占用",
                            f"聯賽快照已寫入 {out_path.name}，但名冊\n"
                            f"{roster_path}\n正被其他程式（通常是 Excel）開啟，"
                            "無法回寫 Last_OCR_ID。\n\n"
                            "請先關閉該 Excel 檔，再按「重試」；"
                            "按「取消」則略過本次名冊回寫。",
                            parent=self,
                        ):
                            continue
                        break
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("roster write-back failed")
                        messagebox.showerror(
                            "名冊回寫失敗",
                            f"快照已寫入 {out_path.name}，但名冊回寫失敗：\n{exc}",
                            parent=self,
                        )
                        break
            finally:
                self._writing = False
                self._refresh_write_state()
            self.app.status_bar.configure(
                text=f"聯賽結果已寫入 {out_path.name}"
                     f"（名冊回寫 {n_updated} 位成員）",
            )
            return out_path

        LeagueReviewDialog(
            self.winfo_toplevel(), result,
            on_confirm=_confirmed, on_closed=self._on_review_closed,
        )

    def _on_review_closed(self, wrote_any: bool) -> None:
        """Review dialog closed. If it wrote at least one snapshot the
        battle is consumed — clear the accumulated scans so screens from
        this battle can never leak into next battle's write-out. When
        nothing was written, keep everything so the user can press
        產出結果 again without recapturing."""
        if not wrote_any:
            return
        self.runner.reset()
        for key in self._cards:
            self._cards[key]["state"] = "idle"
            self._cards[key].pop("progress_text", None)
            self._set_status(key, "尚未拍攝", ("gray10", "gray90"))
        self._refresh_write_state()
        self.app.status_bar.configure(
            text="本場聯賽已產出完成——畫面狀態已重置，可開始拍攝下一場。",
        )
