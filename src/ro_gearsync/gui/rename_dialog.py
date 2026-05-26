"""Modal dialog for renaming a player in the workbook.

Triggered from the main app's "改名/管理成員" toolbar button. Shows a
table-aligned list of every player in the workbook with their highest
gear score and up to three of their most recent day-level entries, so
the user has enough context to pick the right row before renaming.

Layout, top to bottom:

  ┌──────────────────────────────────────────────────────────┐
  │  搜尋：  [____________]                                    │
  ├──────────────────────────────────────────────────────────┤
  │  名字              最高評分    近 3 天裝評                │
  │  ────────────────  ──────────  ───────────────────────  │
  │  杰尼衰              48,800      5/20 48,800 ｜ 5/19 …   │
  │  ...                                                       │
  ├──────────────────────────────────────────────────────────┤
  │  原名字：  杰尼衰                                          │
  │  新名字：  [____________________]                          │
  │                                                            │
  │              [   取消   ]   [ 確認改名 ]                   │
  └──────────────────────────────────────────────────────────┘

After confirming:

  * ``correct_nickname``    ← new value
  * ``latest_ocr_nickname`` ← cleared
  * ``confidence``          ← cleared
  * gear-score history      ← preserved unchanged

Save is deferred to the caller (typically saves immediately so the
rename survives a crash).
"""
from __future__ import annotations

from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

from ..storage import GuildScoresWorkbook


# Column widths in pixels — kept consistent between the header row and
# each data row so the table actually aligns. The CTkScrollableFrame
# strips trailing whitespace from labels, so we can't use string-padded
# columns; we use grid with fixed widths instead.
_COL_ID_W = 50
# Name column shrunk to ~80% per spec (was 200) so the right-side
# gear sub-cells aren't clipped against the scrollable frame edge.
_COL_NAME_W = 160
_COL_PEAK_W = 100
# "近 3 天裝評" splits into 3 pairs of (date, gear) sub-cells so the
# digit columns line up even when one player has 5-digit gear and
# another has 6-digit gear. Date sub-cell holds "M/D", gear sub-cell
# is right-aligned so the comma drifts predictably.
_COL_DATE_W = 42       # fits "12/31" comfortably
_COL_GEAR_W = 70       # fits up to 7-digit "1,234,567" right-aligned
_PAIR_W = _COL_DATE_W + _COL_GEAR_W + 8  # +pad
# Header label width hint only — the data-row recent_frame no longer
# pins itself to this size (grid_propagate is left on so it expands
# to fit the three pair widgets naturally). Without that fix the
# right-most pair was clipped against the locked frame edge.
_COL_RECENT_W = _PAIR_W * 3
_BUTTON_WIDTH = 140


class RenameDialog(ctk.CTkToplevel):
    """A modal window that lets the user pick a player and rename them."""

    def __init__(
        self,
        master: ctk.CTk,
        workbook: GuildScoresWorkbook,
        on_renamed: Callable[[int, str], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.workbook = workbook
        self.on_renamed = on_renamed

        self.title("成員管理")
        self.geometry("840x560")
        self.transient(master)
        self.grab_set()  # modal

        self._all_indices: list[int] = list(range(len(workbook.records)))
        # Build every data row once at startup; filtering just toggles
        # grid visibility (grid_remove / grid) instead of destroying and
        # rebuilding 150+ widgets per keystroke. Eliminates the lag the
        # user noticed with the previous "destroy + rebuild" approach.
        self._row_buttons: dict[int, ctk.CTkFrame] = {}
        self._selected_index: int | None = None

        self._build_ui()
        self._build_all_rows()
        self._refresh_list()

    # --------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ===== Row 0 — search bar =========================================
        search_frame = ctk.CTkFrame(self)
        search_frame.grid(row=0, column=0, padx=16, pady=(16, 6), sticky="ew")
        search_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(search_frame, text="搜尋：", width=64, anchor="w").grid(
            row=0, column=0, padx=(12, 4), pady=10, sticky="w",
        )
        self.search_var = ctk.StringVar()
        self.search_var.trace_add(
            "write", lambda *_: (self._refresh_list(), self._scroll_to_top())
        )
        self.search_entry = ctk.CTkEntry(
            search_frame,
            textvariable=self.search_var,
            placeholder_text="輸入名字片段即可即時過濾",
        )
        self.search_entry.grid(row=0, column=1, padx=(0, 12), pady=10, sticky="ew")

        # ===== Row 1 — table header =======================================
        header = ctk.CTkFrame(self, fg_color=("#e0e0e0", "#333333"))
        header.grid(row=1, column=0, padx=16, pady=(0, 0), sticky="ew")
        self._add_table_cell(header, 0, "ID", width=_COL_ID_W, bold=True, anchor="center")
        self._add_table_cell(header, 1, "名字", width=_COL_NAME_W, bold=True)
        self._add_table_cell(header, 2, "最高評分", width=_COL_PEAK_W, bold=True, anchor="e")
        self._add_table_cell(header, 3, "近 3 天裝評", width=_COL_RECENT_W, bold=True)

        # ===== Row 2 — scrollable list of players =========================
        self._list_frame = ctk.CTkScrollableFrame(self, fg_color=("#fafafa", "#1f1f1f"))
        self._list_frame.grid(row=2, column=0, padx=16, pady=(0, 6), sticky="nsew")
        self._list_frame.grid_columnconfigure(0, weight=0)

        # ===== Row 3 — status hint ========================================
        self.status_label = ctk.CTkLabel(self, text="", text_color="gray", anchor="w")
        self.status_label.grid(row=3, column=0, padx=16, pady=(0, 0), sticky="w")

        # ===== Row 4 — bottom buttons =====================================
        # 改名 is disabled until the user picks a row; clicking pops a
        # secondary modal where the actual 原/新名 inputs live (per spec).
        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=4, column=0, padx=16, pady=(8, 16))
        ctk.CTkButton(
            button_row, text="關閉", width=_BUTTON_WIDTH, command=self.destroy,
        ).grid(row=0, column=0, padx=(0, 8))
        self.rename_btn = ctk.CTkButton(
            button_row, text="改名", width=_BUTTON_WIDTH,
            command=self._open_rename_subdialog,
            state="disabled",
        )
        self.rename_btn.grid(row=0, column=1, padx=(8, 8))
        self.chart_btn = ctk.CTkButton(
            button_row, text="📊 裝評分析", width=_BUTTON_WIDTH,
            command=self._open_gear_chart,
        )
        self.chart_btn.grid(row=0, column=2, padx=(8, 0))

    def _scroll_to_top(self) -> None:
        """Move the list's scroll position back to the top.

        The CTkScrollableFrame embeds a canvas exposed as
        ``_parent_canvas``; yview_moveto(0) puts the view at the top
        unconditionally. Tk needs an idle tick before the just-rebuilt
        rows have valid geometry, so we schedule it with after_idle.
        """
        try:
            canvas = self._list_frame._parent_canvas  # noqa: SLF001
        except AttributeError:
            return
        self.after_idle(lambda: canvas.yview_moveto(0))

    @staticmethod
    def _add_table_cell(
        parent,
        col: int,
        text: str,
        *,
        width: int,
        bold: bool = False,
        anchor: str = "w",
    ) -> ctk.CTkLabel:
        font = ctk.CTkFont(size=12, weight="bold" if bold else "normal")
        label = ctk.CTkLabel(parent, text=text, width=width, anchor=anchor, font=font)
        label.grid(row=0, column=col, padx=4, pady=6, sticky="w")
        return label

    # --------------------------------------------------------------- list

    def _build_all_rows(self) -> None:
        """Eagerly create every row once. Filtering shows/hides them.

        For 150-row workbooks the eager build runs in ~250 ms total but
        every keystroke afterwards stays well under a frame (just grid
        ops, no widget creation).
        """
        for i in self._all_indices:
            row = self._build_data_row(i)
            self._row_buttons[i] = row
            # All rows start hidden — _refresh_list will grid the ones
            # the current filter matches.
            row.grid_remove()
        # Lazily-created "no results" placeholder, hidden by default.
        self._no_results_label = ctk.CTkLabel(
            self._list_frame, text="（沒有符合搜尋的成員）",
            text_color=("#888888", "#777777"),
        )
        self._no_results_label.grid(row=0, column=0, padx=8, pady=12, sticky="w")
        self._no_results_label.grid_remove()

    def _refresh_list(self) -> None:
        q = self.search_var.get().strip().casefold()
        visible_idx = 0
        for i in self._all_indices:
            rec = self.workbook.records[i]
            if q and q not in rec.correct_nickname.casefold():
                self._row_buttons[i].grid_remove()
                continue
            self._row_buttons[i].grid(
                row=visible_idx, column=0, padx=2, pady=1, sticky="ew",
            )
            visible_idx += 1
        # Restore selection highlight if the selected row is still visible.
        if self._selected_index is not None and self._selected_index in self._row_buttons:
            self._highlight(self._selected_index)
        # Empty-state placeholder toggles based on whether anything matched.
        if visible_idx == 0:
            self._no_results_label.grid()
        else:
            self._no_results_label.grid_remove()

    def _build_data_row(self, record_idx: int) -> ctk.CTkFrame:
        """Construct one data row widget. Position is set later by _refresh_list."""
        rec = self.workbook.records[record_idx]
        # Each row is a CTkFrame; click-binding on every cell so the
        # whole row is one click target. CTkButton can't host arbitrary
        # grid children, hence the frame-as-row pattern.
        row = ctk.CTkFrame(
            self._list_frame,
            fg_color=("#ffffff", "#262626"),
            corner_radius=4,
        )

        # ID cell — shows the user-set column-A number, or "—" for
        # phase-5-appended new members who haven't been numbered yet.
        id_text = str(rec.player_id) if rec.player_id is not None else "—"
        id_cell = ctk.CTkLabel(
            row, text=id_text, width=_COL_ID_W, anchor="center",
            text_color=("#666666", "#aaaaaa"),
        )
        id_cell.grid(row=0, column=0, padx=4, pady=4, sticky="w")

        name = rec.correct_nickname or "(未填)"
        peak = f"{rec.peak_gear_score:,}" if rec.peak_gear_score is not None else "—"

        name_cell = ctk.CTkLabel(row, text=name, width=_COL_NAME_W, anchor="w")
        name_cell.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        peak_cell = ctk.CTkLabel(row, text=peak, width=_COL_PEAK_W, anchor="e")
        peak_cell.grid(row=0, column=2, padx=4, pady=4, sticky="w")

        # Recent-days sub-grid: three pairs of (date, gear) cells. Fixed
        # widths + right-aligned gear means digit columns line up even
        # when one player is 5 digits and another is 6 (the original
        # complaint).
        # No grid_propagate(False): the three pair widgets each have
        # fixed widths, so we let the frame size itself to fit them —
        # locking it to _COL_RECENT_W clipped the right-most pair.
        recent_frame = ctk.CTkFrame(row, fg_color="transparent", height=24)
        recent_frame.grid(row=0, column=3, padx=(4, 4), pady=4, sticky="w")

        days = sorted(rec.gear_scores.keys(), reverse=True)[:3]
        sub_cells: list = []
        for col_idx, day_str in enumerate(days):
            # Each (date, gear) pair lives inside its own bordered box so
            # the user can't visually pair a date with the neighbouring
            # row's gear value when columns are of different widths.
            pair = ctk.CTkFrame(
                recent_frame,
                fg_color=("#ffffff", "#262626"),
                border_width=1,
                border_color=("#bbbbbb", "#555555"),
                corner_radius=4,
            )
            pair.grid(row=0, column=col_idx, padx=(4, 0), pady=1, sticky="w")
            d_cell = ctk.CTkLabel(
                pair,
                text=self._short_date(day_str),
                width=_COL_DATE_W,
                anchor="center",
                text_color=("#666666", "#aaaaaa"),
            )
            d_cell.grid(row=0, column=0, padx=(4, 2), pady=2, sticky="w")
            g_cell = ctk.CTkLabel(
                pair,
                text=f"{rec.gear_scores[day_str]:,}",
                width=_COL_GEAR_W,
                anchor="e",
            )
            g_cell.grid(row=0, column=1, padx=(2, 4), pady=2, sticky="e")
            sub_cells.extend([pair, d_cell, g_cell])
        if not days:
            placeholder = ctk.CTkLabel(
                recent_frame, text="—", width=_PAIR_W, anchor="w",
                text_color=("#888888", "#888888"),
            )
            placeholder.grid(row=0, column=0, padx=4, sticky="w")
            sub_cells.append(placeholder)

        for w in (row, id_cell, name_cell, peak_cell, recent_frame, *sub_cells):
            w.bind("<Button-1>", lambda _e, i=record_idx: self._select(i))
        return row

    @staticmethod
    def _short_date(day_str: str) -> str:
        """Convert ``2026-05-18`` → ``5/18``; passthrough if unparsable."""
        try:
            _, month, day = day_str.split("-")
            return f"{int(month)}/{int(day)}"
        except ValueError:
            return day_str

    def _select(self, record_index: int) -> None:
        self._selected_index = record_index
        # 改名 button only becomes clickable once a row is selected —
        # the actual 原/新名 inputs live in a separate popup that
        # opens via _open_rename_subdialog.
        self.rename_btn.configure(state="normal")
        self._highlight(record_index)

    def _highlight(self, record_index: int) -> None:
        for i, row in self._row_buttons.items():
            if i == record_index:
                row.configure(fg_color=("#a5d6ff", "#1f6feb"))
            else:
                row.configure(fg_color=("#ffffff", "#262626"))

    # --------------------------------------------------------------- action

    def _open_rename_subdialog(self) -> None:
        """Open a small modal with 原名字 / 新名字 inputs."""
        if self._selected_index is None:
            return
        rec = self.workbook.records[self._selected_index]
        original = rec.correct_nickname or "(未填)"
        RenameSubDialog(
            self,
            original_name=original,
            on_confirm=lambda new_name: self._commit_rename(new_name),
        )

    def _commit_rename(self, new_name: str) -> None:
        if self._selected_index is None:
            return
        idx = self._selected_index
        # Snapshot the OLD name before rename_player wipes it.
        old_name = self.workbook.records[idx].correct_nickname or "(未填)"
        try:
            self.workbook.rename_player(idx, new_name)
        except Exception as exc:  # noqa: BLE001
            self.status_label.configure(text=f"改名失敗：{exc}", text_color="#cc3333")
            return
        if self.on_renamed is not None:
            self.on_renamed(idx, new_name)
        self.status_label.configure(
            text=f"已將「{old_name}」改名為「{new_name}」",
            text_color="#1d8a3d",
        )

    def _open_gear_chart(self) -> None:
        """Show the matplotlib gear-distribution chart."""
        from .gear_chart_dialog import GearChartDialog
        GearChartDialog(self, workbook=self.workbook)



class RenameSubDialog(ctk.CTkToplevel):
    """Compact secondary modal that opens from the 改名 button.

    Holds the actual 原名字 (read-only label) + 新名字 (entry) so the
    parent member-management view stays clean — list + search + buttons
    only, no leftover input fields that look enabled when nothing is
    selected.
    """

    def __init__(
        self,
        master: ctk.CTk,
        *,
        original_name: str,
        on_confirm,
    ) -> None:
        super().__init__(master)
        self.original_name = original_name
        self.on_confirm = on_confirm

        self.title("改名")
        self.geometry("460x230")
        self.resizable(False, False)
        self.transient(master)
        try:
            self.grab_set()
        except Exception:
            pass

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text="原名字：", width=80, anchor="e").grid(
            row=0, column=0, padx=(16, 8), pady=(22, 6), sticky="e",
        )
        ctk.CTkLabel(
            self, text=self.original_name, anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=1, padx=(0, 16), pady=(22, 6), sticky="w")

        ctk.CTkLabel(self, text="新名字：", width=80, anchor="e").grid(
            row=1, column=0, padx=(16, 8), pady=(8, 16), sticky="e",
        )
        self.new_name_var = ctk.StringVar(value=self.original_name if self.original_name != "(未填)" else "")
        self.new_name_entry = ctk.CTkEntry(
            self, textvariable=self.new_name_var,
            placeholder_text="輸入新名字",
        )
        self.new_name_entry.grid(row=1, column=1, padx=(0, 16), pady=(8, 16), sticky="ew")
        self.new_name_entry.bind("<Return>", lambda _e: self._on_confirm())
        self.after_idle(lambda: (self.new_name_entry.focus_set(),
                                 self.new_name_entry.icursor("end")))

        self.status_label = ctk.CTkLabel(self, text="", text_color="#cc3333")
        self.status_label.grid(row=2, column=0, columnspan=2, padx=16, pady=(0, 4))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=3, column=0, columnspan=2, pady=(8, 18))
        ctk.CTkButton(
            button_row, text="取消", width=120, command=self.destroy,
        ).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(
            button_row, text="確認改名", width=120, command=self._on_confirm,
        ).grid(row=0, column=1, padx=(8, 0))

    def _on_confirm(self) -> None:
        new_name = self.new_name_var.get().strip()
        if not new_name:
            self.status_label.configure(text="新名字不可空白。")
            return
        if not messagebox.askyesno(
            "確認改名",
            (
                f"確定要把：\n\n    {self.original_name}\n\n"
                f"改名為：\n\n    {new_name}\n\n"
                f"確認後會立即寫入 Excel（原檔自動備份）。"
            ),
            parent=self,
        ):
            return
        try:
            self.on_confirm(new_name)
        except Exception as exc:
            self.status_label.configure(text=f"改名失敗：{exc}")
            return
        self.after_idle(self.destroy)
