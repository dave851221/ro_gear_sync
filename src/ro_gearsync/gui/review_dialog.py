"""Post-scan review dialog.

The new flow per spec:

  * Captured rows that didn't exact-match are held back during the scan
    and matched fuzzily at the end. Fuzzy matches go straight into the
    workbook with REVIEW_UNMATCHED (red row) — they're applied but
    flagged for human eyes next time.
  * Anything still unmatched after fuzzy is **never** auto-written.
    This dialog lists them with the source-screenshot row strip so the
    user can eyeball what OCR caught, and offers a per-row dropdown
    (mirroring the league review dialog) to manually assign the capture
    to a missed member — an assignment writes gear AND prepends the OCR
    string to that member's multi-value Last_OCR_ID, so the next scan
    exact-matches. Default is 忽略 (not written).
  * Excel rows that this scan didn't see at all are also listed here,
    each with an editable gear-score field. The user can fill it in
    manually (or leave blank to skip) — only filled values get written.

Two sections, both scrollable (❷ 模糊提案 merged into ❶ 2026-07-10 —
fuzzy rows render their OCR metadata + row-strip thumbnail inline):

  ┌──────────────────────────────────────────────────────────────────┐
  │  分析截圖結果統整 — {date}                                            │
  ├──────────────────────────────────────────────────────────────────┤
  │  ❶ Excel 有但本次沒掃到 (N)  — 手填或勾「套用」模糊提案            │
  │  ────────────────────────────────────────────────────────────── │
  │   #7  阿明   上次 5/19 = 65,800   [✓套用: 69,906]  [手填]  [改名] │
  │       OCR: 阿明6  相似 89%  信心 0.90  📄 page_011.png            │
  │       [———————— row-strip 縮圖 ————————]                         │
  │   ...                                                             │
  ├──────────────────────────────────────────────────────────────────┤
  │  ❷ 辨識到但無 Excel 對應 (M)  — 可下拉指認成員，或忽略            │
  │  ────────────────────────────────────────────────────────────── │
  │   緊張爺爺  85,493  conf 0.86  page_009.png   指認為: [▼ 忽略]    │
  │   ...                                                             │
  ├──────────────────────────────────────────────────────────────────┤
  │                       [取消]   [確認並儲存]                       │
  └──────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

import customtkinter as ctk
from PIL import Image


@dataclass
class MissedReviewItem:
    """One row in section ❶ of the review dialog.

    Two flavours:
      * **Pure missed** — workbook had this row, OCR didn't see anything
        for it this scan. Renders with a manual gear-score input only.
        ``candidate_gear`` is ``None``.
      * **Phase-3 fuzzy candidate** — workbook had this row, OCR found
        a similar string and fuzzy-matched (≥ 65). Renders with an
        opt-in 套用 checkbox showing the proposed gear, plus the
        manual gear-score input (used when the user does NOT tick 套用).
        ``candidate_gear`` is non-None.
    """
    record_index: int
    name: str                  # correct_nickname (or fallback)
    last_day: str | None       # most recent day with a value
    last_value: int | None     # the value on last_day
    # Phase-3 candidate context — populated only for fuzzy hits.
    candidate_gear: int | None = None
    candidate_ocr_name: str | None = None
    candidate_score: float | None = None
    candidate_confidence: float | None = None
    candidate_page: int | None = None
    candidate_row_y: int | None = None
    # Full path to ``page_NNN.png`` for the fuzzy proposal's source
    # screenshot, so the dialog can crop a row-strip thumbnail (same
    # treatment the ❸ unmatched section gets).
    candidate_image_path: "Path | None" = None


@dataclass
class FuzzyApproval:
    """User-approved phase-3 fuzzy match, returned by ReviewDialog."""
    record_index: int
    gear_score: int
    ocr_nickname: str
    confidence: float | None


@dataclass
class ReviewDecisions:
    """ReviewDialog → caller payload.

    * ``cancelled`` — True when the user explicitly cancelled (取消 or
      ✕). Caller MUST skip every workbook write when this is set —
      including phase-1/2 exact matches that would otherwise be safe.
      No backup is taken either; the workbook stays exactly as it was
      before the scan started.
    * ``manual_gear`` — record_index → typed gear value (only entries
      the user actually filled in). Applies to both pure missed rows
      and fuzzy candidates the user chose to override instead of
      ticking 套用. Ignored when ``cancelled`` is True.
    * ``approved_fuzzy`` — fuzzy candidates the user ticked 套用 on.
      The caller writes both the gear AND updates
      ``latest_ocr_nickname`` on the matched record. Ignored when
      ``cancelled`` is True.
    * ``assigned_unmatched`` — ❷-section captures the user manually
      assigned to a missed member via the dropdown. Same write
      semantics as ``approved_fuzzy`` (gear + Last_OCR_ID prepend) —
      reuses :class:`FuzzyApproval` with ``confidence`` carrying the
      OCR confidence. Ignored when ``cancelled`` is True.

    The on_confirm callback is expected to return ``True`` (or
    ``None``) on success, ``False`` if the write failed and the
    dialog should stay open so the user can retry.
    """
    manual_gear: dict[int, int]
    approved_fuzzy: list[FuzzyApproval]
    assigned_unmatched: list[FuzzyApproval] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class UnmatchedReviewItem:
    """One OCR capture that didn't match any record (info only).

    ``image_path`` + ``row_y`` are optional but, when provided, let the
    dialog crop a thumbnail of the original row so the user can visually
    verify what the OCR missed.
    """
    ocr_nickname: str
    gear_score: int
    confidence: float | None
    page_index: int | None
    image_path: "Path | None" = None
    row_y: int | None = None


# Column widths shared between header and data rows so the table aligns.
_M_ID_W = 50              # column-A player_id ("#42")
_M_NAME_W = 140
_M_LAST_W = 130
# Fuzzy-candidate checkbox column. The checkbox text is a FIXED short
# format ("套用: 1,234,567") — the variable-length OCR metadata lives on
# the thumbnail sub-row instead, so this column can never push the
# 手填/改名 columns out of alignment (the pre-2026-07-10 bug).
_M_CAND_W = 150
_M_INPUT_W = 110
_M_RENAME_W = 70          # per-row 改名 button
_U_NAME_W = 160
_U_GEAR_W = 90
_U_CONF_W = 70
_U_PAGE_W = 130
# Thumbnail size for the unmatched-rows photo strip. A typical LDPlayer
# screencap is 1920×80 (row band), so the display size has to be a
# compromise between "readable text" and "fits in the dialog". 760×60
# stretches characters slightly vertically but keeps both name and
# gear-score columns legible at a glance.
_U_THUMB_W = 760
_U_THUMB_H = 60
# Inline thumbnail in a ❶ fuzzy row must NOT exceed the six-column
# total (~698px incl. padding) or the spanning image would widen the
# columns of that row and break cross-row alignment.
_M_THUMB_W = 690
_M_THUMB_H = 54
_BUTTON_W = 140

# Default (no-op) choice in the ❷ assignment dropdown.
IGNORE_LABEL = "（忽略此筆，不寫入）"


class ReviewDialog(ctk.CTkToplevel):
    """Modal shown after a scan finishes (live or rescan)."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        capture_day: str,
        missed: list[MissedReviewItem],
        unmatched: list[UnmatchedReviewItem],
        on_confirm: Callable[[ReviewDecisions], None],
        workbook=None,                       # for per-row rename
        on_renamed: Callable[[int, str], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.capture_day = capture_day
        # ❶ owns ALL the missed rows (interactive: 套用 / 手填 / 改名).
        # Fuzzy-hit rows render their OCR metadata + row-strip thumbnail
        # inline (the standalone ❷ 模糊提案 section was merged in here
        # 2026-07-10), so the user verifies and decides in one place.
        self.missed = missed
        self.unmatched = unmatched
        self.on_confirm = on_confirm
        # The workbook + rename callback let each ❶ row offer an inline
        # 改名 button (a member often shows up here because they
        # in-game-renamed since the last scan).
        self.workbook = workbook
        self.on_renamed = on_renamed
        # Once the user successfully saves at least once, this flips to
        # True so the X / 取消 path can skip the "everything will be
        # discarded" confirmation (nothing left to discard).
        self._saved_once: bool = False
        # record_index → CTkEntry for the manual gear input (all rows).
        self._missed_entries: dict[int, ctk.CTkEntry] = {}
        # record_index → BooleanVar for the 套用 checkbox (only fuzzy
        # candidate rows have an entry here).
        self._apply_vars: dict[int, ctk.BooleanVar] = {}
        # Per-❷-row assignment state: unmatched-list index →
        # (StringVar, {dropdown label: record_index}).
        self._assign_vars: dict[int, tuple[ctk.StringVar, dict[str, int]]] = {}
        # Per-row 改名 button refs so we can update label text after rename.
        self._row_widgets: dict[int, dict] = {}
        # Holds CTkImage thumbnails alive — Tk image references are weak,
        # so without an explicit cache they'd be GC'd before paint.
        self._thumbnail_cache: list[ctk.CTkImage] = []

        self.title(f"分析截圖結果統整 — {capture_day}")
        # Wide enough for ❶'s extra fuzzy-candidate column AND ❷'s
        # row-strip thumbnails (760px each).
        self.geometry("1000x760")
        # No transient / grab_set: per spec, this window should NOT be
        # modal — the user wants to click freely between this and the
        # main window (e.g. to scroll the live table while filling in
        # gear scores). Removing grab_set also restores draggable
        # scrollbars in the main window while the dialog is open.

        # Intercept the window-close (✕) button so users can't silently
        # abandon a partly-filled missed list. Routes through the same
        # guard as the 取消 button.
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._build_ui()

    # --------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        # Two scrollable sections — ❶ gets the bigger share of the
        # vertical budget (fuzzy rows carry inline thumbnails now).
        self.grid_rowconfigure(1, weight=3)
        self.grid_rowconfigure(3, weight=2)

        # ===== ❶ Missed rows (pure missed + inline fuzzy proposals) =====
        n_fuzzy = sum(1 for m in self.missed if m.candidate_gear is not None)
        missed_header = ctk.CTkFrame(self, fg_color=("#e0e0e0", "#333333"))
        missed_header.grid(row=0, column=0, padx=12, pady=(12, 0), sticky="ew")
        ctk.CTkLabel(
            missed_header,
            text=(
                f"❶ Excel 有但本次沒掃到 ({len(self.missed)} 位，"
                f"其中 {n_fuzzy} 位有模糊提案) — 勾「套用」寫入提案值"
                "（下方附截圖供核對），或手填本次裝評；留空則略過。"
            ),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        self._missed_frame = ctk.CTkScrollableFrame(
            self, fg_color=("#fafafa", "#1f1f1f"),
            label_text=None,
        )
        self._missed_frame.grid(row=1, column=0, padx=12, pady=0, sticky="nsew")
        self._missed_frame.grid_columnconfigure(0, weight=0)

        self._populate_missed()

        # ===== ❷ Unmatched (OCR saw it, no Excel match) =====
        unmatched_header = ctk.CTkFrame(self, fg_color=("#e0e0e0", "#333333"))
        unmatched_header.grid(row=2, column=0, padx=12, pady=(12, 0), sticky="ew")
        ctk.CTkLabel(
            unmatched_header,
            text=(
                f"❷ 辨識到但無 Excel 對應 ({len(self.unmatched)} 位) "
                "— 可用下拉選單指認為 ❶ 的成員（會寫入裝評），"
                "或維持「忽略」不寫入。"
            ),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        self._unmatched_frame = ctk.CTkScrollableFrame(
            self, fg_color=("#fafafa", "#1f1f1f"),
            label_text=None,
        )
        self._unmatched_frame.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="nsew")
        self._unmatched_frame.grid_columnconfigure(0, weight=0)

        self._populate_unmatched()

        # ===== Buttons =====
        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=4, column=0, padx=12, pady=(4, 16))
        ctk.CTkButton(
            button_row, text="取消", width=_BUTTON_W, command=self._on_cancel,
        ).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(
            button_row, text="確認並儲存", width=_BUTTON_W, command=self._on_ok,
        ).grid(row=0, column=1, padx=(8, 0))

    def _populate_missed(self) -> None:
        if not self.missed:
            ctk.CTkLabel(
                self._missed_frame, text="（沒有缺席的成員、也沒有需要確認的模糊比對）",
                text_color=("#888888", "#777777"),
            ).grid(row=0, column=0, padx=8, pady=12, sticky="w")
            return

        # Header row — added ID col on left + 改名 col on right per spec.
        # Header and every data row share _configure_missed_columns so
        # the six columns line up regardless of per-row content.
        h = ctk.CTkFrame(self._missed_frame, fg_color="transparent")
        h.grid(row=0, column=0, padx=2, pady=(2, 4), sticky="w")
        self._configure_missed_columns(h)
        bold = ctk.CTkFont(size=12, weight="bold")
        ctk.CTkLabel(h, text="ID", width=_M_ID_W, anchor="center", font=bold).grid(
            row=0, column=0, padx=4)
        ctk.CTkLabel(h, text="名字", width=_M_NAME_W, anchor="w", font=bold).grid(
            row=0, column=1, padx=4, sticky="w")
        ctk.CTkLabel(h, text="上次紀錄", width=_M_LAST_W, anchor="w", font=bold).grid(
            row=0, column=2, padx=4, sticky="w")
        ctk.CTkLabel(h, text="模糊提案", width=_M_CAND_W, anchor="w", font=bold).grid(
            row=0, column=3, padx=4, sticky="w")
        ctk.CTkLabel(h, text="或手填裝評", width=_M_INPUT_W, anchor="w", font=bold).grid(
            row=0, column=4, padx=4, sticky="w")
        ctk.CTkLabel(h, text="操作", width=_M_RENAME_W, anchor="center", font=bold).grid(
            row=0, column=5, padx=4)

        for r_idx, item in enumerate(self.missed, start=1):
            is_fuzzy = item.candidate_gear is not None
            row = ctk.CTkFrame(
                self._missed_frame,
                fg_color=("#ffffff", "#262626"),
                border_width=1 if is_fuzzy else 0,
                border_color=("#cccccc", "#444444"),
            )
            row.grid(row=r_idx, column=0, padx=2, pady=(4 if is_fuzzy else 1), sticky="w")
            self._configure_missed_columns(row)

            # ID column
            rec = (self.workbook.records[item.record_index]
                   if self.workbook and 0 <= item.record_index < len(self.workbook.records)
                   else None)
            id_text = str(rec.player_id) if (rec and rec.player_id is not None) else "—"
            ctk.CTkLabel(
                row, text=id_text, width=_M_ID_W, anchor="center",
                text_color=("#666666", "#aaaaaa"),
            ).grid(row=0, column=0, padx=4, pady=4)

            name_label = ctk.CTkLabel(row, text=item.name, width=_M_NAME_W, anchor="w")
            name_label.grid(row=0, column=1, padx=4, pady=4, sticky="w")

            # Last-record column
            if item.last_day and item.last_value is not None:
                try:
                    _, m, d = item.last_day.split("-")
                    short = f"{int(m)}/{int(d)}"
                except ValueError:
                    short = item.last_day
                last_text = f"{short} = {item.last_value:,}"
            else:
                last_text = "（無紀錄）"
            ctk.CTkLabel(
                row, text=last_text, width=_M_LAST_W, anchor="w",
                text_color=("#666666", "#bbbbbb"),
            ).grid(row=0, column=2, padx=4, pady=4, sticky="w")

            # Fuzzy candidate column — only render the 套用 checkbox
            # for rows where the matcher found a phase-3 fuzzy hit.
            # Checkbox text is the fixed-format proposal value only; the
            # OCR name / similarity / confidence / thumbnail render on
            # the sub-rows below so this column stays a constant width.
            entry = ctk.CTkEntry(row, width=_M_INPUT_W, placeholder_text="留空略過")
            if is_fuzzy:
                apply_var = ctk.BooleanVar(value=False)

                def _on_toggle(*_args, idx=item.record_index, var=apply_var, e=entry):
                    if var.get():
                        e.delete(0, "end")
                        e.configure(state="disabled")
                    else:
                        e.configure(state="normal")

                apply_var.trace_add("write", _on_toggle)
                self._apply_vars[item.record_index] = apply_var
                ctk.CTkCheckBox(
                    row,
                    text=f"套用: {item.candidate_gear:,}",
                    variable=apply_var,
                    width=_M_CAND_W,
                    text_color=("#1d6f3a", "#7cd693"),
                ).grid(row=0, column=3, padx=4, pady=4, sticky="w")
            else:
                ctk.CTkLabel(
                    row, text="—", width=_M_CAND_W, anchor="w",
                    text_color=("#888888", "#777777"),
                ).grid(row=0, column=3, padx=4, pady=4, sticky="w")

            entry.grid(row=0, column=4, padx=4, pady=4, sticky="w")
            self._missed_entries[item.record_index] = entry

            # Per-row 改名 button — opens the RenameSubDialog scoped
            # to this record. Useful when a member shows up in section
            # ❶ because they renamed in-game since the previous scan.
            rename_btn = ctk.CTkButton(
                row, text="改名", width=_M_RENAME_W, height=24,
                command=lambda i=item.record_index, lbl=name_label:
                    self._open_inline_rename(i, lbl),
                state="normal" if (self.workbook and self.on_renamed) else "disabled",
            )
            rename_btn.grid(row=0, column=5, padx=4, pady=4)
            self._row_widgets[item.record_index] = {
                "name_label": name_label, "rename_btn": rename_btn,
            }

            if is_fuzzy:
                self._add_fuzzy_detail_rows(row, item)

    @staticmethod
    def _configure_missed_columns(frame) -> None:
        """Pin the six ❶-table column widths on ``frame``.

        Every row is its own CTkFrame, so without a shared minsize a
        wide widget in one row would shift its neighbours relative to
        other rows (the pre-2026-07-10 misalignment).
        """
        widths = (_M_ID_W, _M_NAME_W, _M_LAST_W, _M_CAND_W, _M_INPUT_W, _M_RENAME_W)
        for col, w in enumerate(widths):
            frame.grid_columnconfigure(col, minsize=w + 8)  # +8 = padx*2

    def _add_fuzzy_detail_rows(self, row, item: MissedReviewItem) -> None:
        """Append OCR metadata + row-strip thumbnail beneath a fuzzy row.

        This is the old standalone ❷ 模糊提案 card content, rendered
        inline (2026-07-10) so the user sees proposal + evidence +
        controls in one place.
        """
        meta = ctk.CTkFrame(row, fg_color="transparent")
        meta.grid(row=1, column=1, columnspan=5, padx=4, pady=(0, 2), sticky="w")
        ctk.CTkLabel(
            meta, text=f"OCR: {item.candidate_ocr_name or '(空)'}", anchor="w",
        ).grid(row=0, column=0, padx=(0, 12), sticky="w")
        if item.candidate_score is not None:
            ctk.CTkLabel(
                meta,
                text=f"相似 {item.candidate_score:.0f}%",
                text_color=("#1d6f3a", "#7cd693"),
            ).grid(row=0, column=1, padx=(0, 12), sticky="w")
        if item.candidate_confidence is not None:
            ctk.CTkLabel(
                meta,
                text=f"信心 {item.candidate_confidence:.2f}",
                text_color=("#666666", "#bbbbbb"),
            ).grid(row=0, column=2, padx=(0, 12), sticky="w")
        if item.candidate_page is not None:
            ctk.CTkLabel(
                meta,
                text=f"📄 page_{item.candidate_page:03d}.png",
                text_color=("#666666", "#bbbbbb"),
            ).grid(row=0, column=3, padx=(0, 0), sticky="w")

        thumb = self._make_row_thumbnail(
            item.candidate_image_path, item.candidate_row_y,
            size=(_M_THUMB_W, _M_THUMB_H),
        )
        if thumb is not None:
            ctk.CTkLabel(row, text="", image=thumb).grid(
                row=2, column=0, columnspan=6, padx=8, pady=(0, 6), sticky="w",
            )
        else:
            ctk.CTkLabel(
                row,
                text="(找不到原始截圖)",
                anchor="w",
                text_color=("#888888", "#777777"),
            ).grid(row=2, column=0, columnspan=6, padx=8, pady=(0, 6), sticky="w")

    def _open_inline_rename(self, record_index: int, name_label) -> None:
        """Open RenameSubDialog for ❶-section per-row rename button."""
        if self.workbook is None or self.on_renamed is None:
            return
        from .rename_dialog import RenameSubDialog
        rec = self.workbook.records[record_index]
        original = rec.correct_nickname or "(未填)"

        def _commit(new_name: str) -> None:
            try:
                self.workbook.rename_player(record_index, new_name)
            except Exception:
                return
            # Update inline UI + bubble up to the parent app for save.
            try:
                name_label.configure(text=new_name)
            except Exception:
                pass
            if self.on_renamed is not None:
                self.on_renamed(record_index, new_name)

        RenameSubDialog(self, original_name=original, on_confirm=_commit)

    def _assignment_candidates(self) -> dict[str, int]:
        """Dropdown label → record_index for the ❷ assignment combobox.

        Candidates are exactly the ❶ missed members (everyone the scan
        did NOT exact-match) — an unmatched capture can only plausibly
        belong to someone who has no data this round. ID-sorted like the
        league dialog.
        """
        def _pid(item: MissedReviewItem) -> int | None:
            rec = (self.workbook.records[item.record_index]
                   if self.workbook and 0 <= item.record_index < len(self.workbook.records)
                   else None)
            return rec.player_id if rec else None

        entries = sorted(
            ((pid, m) for m in self.missed for pid in [_pid(m)]),
            key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0),
        )
        return {
            f"{pid if pid is not None else '—'}｜{m.name}": m.record_index
            for pid, m in entries
        }

    def _populate_unmatched(self) -> None:
        if not self.unmatched:
            ctk.CTkLabel(
                self._unmatched_frame, text="（全部辨識到的人都已配對成功）",
                text_color=("#888888", "#777777"),
            ).grid(row=0, column=0, padx=8, pady=12, sticky="w")
            return

        candidates = self._assignment_candidates()

        # Card layout per spec — each unmatched capture gets its own
        # bordered card with metadata on top and a large row-strip
        # screenshot beneath. The whole ❷ section is scrollable so the
        # user can flip through them all without leaving the dialog.
        for r_idx, item in enumerate(self.unmatched):
            card = ctk.CTkFrame(
                self._unmatched_frame,
                fg_color=("#ffffff", "#262626"),
                border_width=1,
                border_color=("#cccccc", "#444444"),
            )
            card.grid(row=r_idx, column=0, padx=4, pady=6, sticky="w")

            # ---- top: metadata strip --------------------------------------
            meta = ctk.CTkFrame(card, fg_color="transparent")
            meta.grid(row=0, column=0, padx=8, pady=(6, 2), sticky="w")
            name = item.ocr_nickname or "(空)"
            # OCR name is shown in a readonly Entry so the user can
            # mouse-select and copy it (a CTkLabel can't be selected).
            # We use the bare tkinter Entry (not CTkEntry) for full
            # readonly + no-border control. Visually styled to mimic
            # the surrounding label fonts so it doesn't look like a
            # textbox the user can edit into.
            import tkinter as _tk
            ocr_var = _tk.StringVar(value=name)
            ocr_entry = _tk.Entry(
                meta,
                textvariable=ocr_var,
                state="readonly",
                readonlybackground=card.cget("fg_color")[0]
                    if isinstance(card.cget("fg_color"), tuple) else "white",
                relief="flat",
                bd=0,
                highlightthickness=0,
                font=("Microsoft JhengHei", 12, "bold"),
                width=int(_U_NAME_W / 8),
                cursor="ibeam",
            )
            ocr_entry.grid(row=0, column=0, padx=(0, 6), sticky="w")
            ctk.CTkLabel(
                meta, text=f"裝評: {item.gear_score:,}", width=_U_GEAR_W + 30,
                anchor="w",
            ).grid(row=0, column=1, padx=4, sticky="w")
            conf_text = (
                f"信心: {item.confidence:.2f}" if item.confidence is not None
                else "信心: —"
            )
            ctk.CTkLabel(
                meta, text=conf_text, width=_U_CONF_W + 20, anchor="w",
                text_color=("#666666", "#bbbbbb"),
            ).grid(row=0, column=2, padx=4, sticky="w")
            page_text = (
                f"page_{item.page_index:03d}.png"
                if item.page_index is not None else "—"
            )
            # Readonly Entry so the user can copy the filename out
            # (handy when they want to open the PNG in an image viewer).
            page_var = _tk.StringVar(value=f"📄 {page_text}")
            page_entry = _tk.Entry(
                meta, textvariable=page_var, state="readonly",
                readonlybackground=card.cget("fg_color")[0]
                    if isinstance(card.cget("fg_color"), tuple) else "white",
                relief="flat", bd=0, highlightthickness=0,
                font=("Microsoft JhengHei", 10),
                width=int((_U_PAGE_W + 30) / 7),
                cursor="ibeam", fg="#666666",
            )
            page_entry.grid(row=0, column=3, padx=4, sticky="w")

            # ---- middle: manual assignment dropdown ----------------------
            # Mirrors the league review dialog (2026-07-10): the user can
            # assign this capture to a ❶ missed member. Default 忽略 =
            # legacy behaviour (not written). ttk.Combobox over
            # CTkOptionMenu for the wheel-scrollable native popdown.
            assign_row = ctk.CTkFrame(card, fg_color="transparent")
            assign_row.grid(row=1, column=0, padx=8, pady=(0, 2), sticky="w")
            ctk.CTkLabel(
                assign_row, text="指認為 ❶ 的成員：", anchor="w",
            ).grid(row=0, column=0, padx=(0, 6), sticky="w")
            var = ctk.StringVar(value=IGNORE_LABEL)
            combo = ttk.Combobox(
                assign_row, textvariable=var,
                values=[IGNORE_LABEL, *candidates],
                state="readonly", width=32, height=18,
            )
            combo.grid(row=0, column=1, ipady=2)
            # Wheel must scroll ONLY inside the opened popdown — a closed
            # readonly combobox on Windows cycles values on wheel, which
            # would silently change an assignment while scrolling the
            # dialog. 'break' stops the class binding; the popdown is a
            # separate toplevel Listbox so its own scrolling still works.
            combo.bind("<MouseWheel>", lambda e: "break")
            combo.bind(
                "<<ComboboxSelected>>",
                lambda e, u=r_idx: self._on_assignment_selected(u),
            )
            self._assign_vars[r_idx] = (var, candidates)

            # ---- bottom: row-strip screenshot ----------------------------
            thumb = self._make_row_thumbnail(item.image_path, item.row_y)
            if thumb is not None:
                ctk.CTkLabel(card, text="", image=thumb).grid(
                    row=2, column=0, padx=8, pady=(2, 8), sticky="w",
                )
            else:
                ctk.CTkLabel(
                    card,
                    text="(找不到原始截圖)",
                    width=_U_THUMB_W,
                    anchor="w",
                    text_color=("#888888", "#777777"),
                ).grid(row=2, column=0, padx=8, pady=(2, 8), sticky="w")

    def _make_row_thumbnail(
        self,
        image_path: "Path | None",
        row_y: int | None,
        size: tuple[int, int] = (_U_THUMB_W, _U_THUMB_H),
    ) -> "ctk.CTkImage | None":
        """Crop a horizontal band around ``row_y`` and shrink for display.

        Caches the resulting :class:`ctk.CTkImage` on the dialog so Tk's
        weak image references don't drop it before the next paint pass.
        Returns ``None`` if anything goes wrong — the caller renders a
        text placeholder in that case.
        """
        if image_path is None or row_y is None or not image_path.is_file():
            return None
        try:
            img = Image.open(image_path)
            # ``row_y`` from the parser is the y-coordinate of the gear
            # text anchor (mid-row). A 100-px tall band (50 above + 50
            # below) reliably catches the whole in-game row including
            # the avatar to the left and the gear digits on the right.
            half_h = 50
            top = max(0, row_y - half_h)
            bottom = min(img.height, row_y + half_h)
            if bottom <= top:
                return None
            band = img.crop((0, top, img.width, bottom))
            # CTkImage handles its own resize on render. We hand it the
            # band at native crop resolution and pin the display size.
            ctk_img = ctk.CTkImage(
                light_image=band,
                dark_image=band,
                size=size,
            )
            self._thumbnail_cache.append(ctk_img)
            return ctk_img
        except Exception:
            return None

    # --------------------------------------------------------------- assignment

    def _assignment_conflicts(
        self, record_index: int, *, exclude_uidx: int | None = None,
    ) -> list[str]:
        """Why assigning an ❷ capture to ``record_index`` clashes.

        Three clash flavours, all meaning "this member is already getting
        a gear value from somewhere else this round":

          * their ❶ 套用 checkbox is ticked (fuzzy proposal approved)
          * their ❶ manual gear entry has a value typed in
          * another ❷ capture is already assigned to them
        """
        conflicts: list[str] = []
        apply_var = self._apply_vars.get(record_index)
        if apply_var is not None and apply_var.get():
            conflicts.append("❶ 已勾選「套用」模糊提案")
        entry = self._missed_entries.get(record_index)
        if entry is not None:
            try:
                if entry.get().strip():
                    conflicts.append("❶ 已手填裝評")
            except Exception:
                pass
        for uidx, (var, cand_map) in self._assign_vars.items():
            if uidx == exclude_uidx:
                continue
            if cand_map.get(var.get()) == record_index:
                other = self.unmatched[uidx]
                conflicts.append(
                    f"❷ 的「{other.ocr_nickname or '(空)'}」已指認給此成員"
                )
        return conflicts

    def _on_assignment_selected(self, uidx: int) -> None:
        """Combobox change handler — warn (not block) on clashes.

        The user might intend to resolve the clash next (e.g. untick
        套用), so selection only warns; ``_on_ok`` enforces.
        """
        var, cand_map = self._assign_vars[uidx]
        record_index = cand_map.get(var.get())
        if record_index is None:
            return
        conflicts = self._assignment_conflicts(record_index, exclude_uidx=uidx)
        if conflicts:
            name = next(
                (m.name for m in self.missed if m.record_index == record_index),
                "(未知)",
            )
            messagebox.showwarning(
                "該成員已有本次資料",
                (
                    f"成員「{name}」已經會在本次寫入資料：\n"
                    + "\n".join(f"  • {c}" for c in conflicts)
                    + "\n\n同一位成員一次掃描只能有一筆裝評。\n"
                    "請在按「確認並儲存」前擇一保留"
                    "（取消勾選／清空手填／把其中一邊改回忽略）。"
                ),
                parent=self,
            )

    # --------------------------------------------------------------- actions

    def _on_cancel(self) -> None:
        # Cancel / ✕ — per spec, this discards EVERYTHING. Even rows
        # that exact-matched successfully are NOT written. Workbook
        # state stays exactly as it was before the scan started.
        #
        # Once the user has already saved at least once in this session
        # (``_saved_once`` flips True in ``_on_ok``), the discard
        # confirmation is misleading — there is nothing left to throw
        # away — so we close silently in that case.
        if not self._saved_once:
            if not messagebox.askyesno(
                "確認取消",
                (
                    "取消後本次分析的結果將不會寫入 Excel，\n"
                    "包含已配對成功的成員資料也不會儲存。\n\n"
                    "確定要取消嗎？"
                ),
                parent=self,
            ):
                return
            # cancelled=True tells the app to skip merge_capture + save.
            self.on_confirm(ReviewDecisions(
                manual_gear={}, approved_fuzzy=[], cancelled=True,
            ))
        # Defer destroy to next idle so any pending CTkEntry focus
        # animations / trace callbacks have a chance to complete on
        # widgets that still exist (avoids the
        # ``bad window path name ... .!ctkentry.!entry`` TclError).
        self.after_idle(self.destroy)

    def _any_missed_input_filled(self) -> bool:
        for entry in self._missed_entries.values():
            try:
                if entry.get().strip():
                    return True
            except Exception:  # entry might be in disabled state
                continue
        return False

    def _any_fuzzy_approved(self) -> bool:
        return any(v.get() for v in self._apply_vars.values())

    def _on_ok(self) -> None:
        manual_gear: dict[int, int] = {}
        approved_fuzzy: list[FuzzyApproval] = []
        # Index missed items by record_index for the candidate-info lookup.
        by_idx = {m.record_index: m for m in self.missed}

        for idx, entry in self._missed_entries.items():
            # Branch 1: user ticked 套用 on a fuzzy candidate.
            apply_var = self._apply_vars.get(idx)
            if apply_var is not None and apply_var.get():
                item = by_idx.get(idx)
                if item is None or item.candidate_gear is None:
                    continue
                approved_fuzzy.append(FuzzyApproval(
                    record_index=idx,
                    gear_score=item.candidate_gear,
                    ocr_nickname=item.candidate_ocr_name or "",
                    confidence=item.candidate_confidence,
                ))
                continue
            # Branch 2: user typed a gear value (manual override or pure missed).
            try:
                text = entry.get().strip()
            except Exception:  # entry may be disabled (套用 ticked then untoggled)
                text = ""
            if not text:
                continue
            try:
                cleaned = text.replace(",", "").replace(" ", "")
                manual_gear[idx] = int(cleaned)
            except ValueError:
                entry.configure(border_color="#cc3333")
                return

        # ❷ assignments — collect, then hard-block unresolved clashes
        # (selection time only warned; a member must not receive two
        # gear values in one scan, and 套用/手填/指認 are exclusive).
        assigned_unmatched: list[FuzzyApproval] = []
        blockers: list[str] = []
        for uidx, (var, cand_map) in self._assign_vars.items():
            record_index = cand_map.get(var.get())
            if record_index is None:
                continue  # 忽略 — not written (legacy behaviour)
            item = self.unmatched[uidx]
            conflicts = self._assignment_conflicts(record_index, exclude_uidx=uidx)
            if conflicts:
                name = next(
                    (m.name for m in self.missed if m.record_index == record_index),
                    "(未知)",
                )
                blockers.append(
                    f"「{item.ocr_nickname or '(空)'}」→ 成員「{name}」："
                    + "；".join(conflicts)
                )
                continue
            assigned_unmatched.append(FuzzyApproval(
                record_index=record_index,
                gear_score=item.gear_score,
                ocr_nickname=item.ocr_nickname or "",
                confidence=item.confidence,
            ))
        if blockers:
            messagebox.showerror(
                "指認衝突",
                (
                    "以下指認的成員同時還有其他資料來源，"
                    "同一位成員一次掃描只能寫入一筆裝評：\n\n"
                    + "\n".join(blockers)
                    + "\n\n請擇一保留（取消勾選「套用」／清空手填／"
                    "把其中一個指認改回忽略）後再按確認。"
                ),
                parent=self,
            )
            return

        # If the caller returns False, the workbook write failed (e.g.
        # Excel was holding the file open). Keep the review dialog
        # alive so the user can fix it and click 確認並儲存 again
        # rather than losing all their manual inputs.
        result = self.on_confirm(ReviewDecisions(
            manual_gear=manual_gear, approved_fuzzy=approved_fuzzy,
            assigned_unmatched=assigned_unmatched,
        ))
        if result is False:
            return
        # Per spec: do NOT auto-close on successful save. The user may
        # spot a row that still needs editing AFTER the SaveSuccessDialog
        # closes; leaving this window open lets them fix it and re-submit
        # without redoing the entire scan. They close it manually when
        # done. ``_saved_once`` flips the X / 取消 path to skip the
        # discard confirmation (there's nothing left to discard).
        self._saved_once = True
