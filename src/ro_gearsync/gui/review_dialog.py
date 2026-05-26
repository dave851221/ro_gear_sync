"""Post-scan review dialog.

The new flow per spec:

  * Captured rows that didn't exact-match are held back during the scan
    and matched fuzzily at the end. Fuzzy matches go straight into the
    workbook with REVIEW_UNMATCHED (red row) — they're applied but
    flagged for human eyes next time.
  * Anything still unmatched after fuzzy is **never** written. Instead
    this dialog lists them so the user can see what OCR caught that
    doesn't belong to any known player. Each entry shows which page
    screenshot it came from so the user can verify.
  * Excel rows that this scan didn't see at all are also listed here,
    each with an editable gear-score field. The user can fill it in
    manually (or leave blank to skip) — only filled values get written.

Two sections, both scrollable:

  ┌──────────────────────────────────────────────────────────────────┐
  │  分析截圖結果統整 — {date}                                            │
  ├──────────────────────────────────────────────────────────────────┤
  │  Excel 有但本次沒掃到 (N)  — 您可手動填入裝評，留空略過            │
  │  ────────────────────────────────────────────────────────────── │
  │   阿明      上次 5/19 = 65,800     本次裝評: [____________]      │
  │   ...                                                             │
  ├──────────────────────────────────────────────────────────────────┤
  │  辨識到但無 Excel 對應 (M)  — 僅供參考，不會寫入                  │
  │  ────────────────────────────────────────────────────────────── │
  │   緊張爺爺      85,493      conf 0.86     page_009.png            │
  │   ...                                                             │
  ├──────────────────────────────────────────────────────────────────┤
  │                       [取消]   [確認並儲存]                       │
  └──────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox
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

    The on_confirm callback is expected to return ``True`` (or
    ``None``) on success, ``False`` if the write failed and the
    dialog should stay open so the user can retry.
    """
    manual_gear: dict[int, int]
    approved_fuzzy: list[FuzzyApproval]
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
_M_CAND_W = 220           # fuzzy candidate info ("套用: 12,345 ← OCR")
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
_BUTTON_W = 140


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
        # ❶ still owns ALL the missed rows (interactive: 套用 / 手填 /
        # 改名). ❷ is a read-only visual aid that surfaces the source
        # screenshot + OCR metadata for the fuzzy-hit subset, so the
        # user can verify the proposed gear belongs to the right person
        # before deciding whether to tick 套用 in ❶.
        self.missed = missed
        self.fuzzy_proposals: list[MissedReviewItem] = [
            m for m in missed if m.candidate_gear is not None
        ]
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
        # Three scrollable sections — share the vertical budget so the
        # dialog stays usable whether the user has 1 missed row or 50.
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # ===== ❶ Pure missed (Excel had it, OCR saw nothing) =====
        missed_header = ctk.CTkFrame(self, fg_color=("#e0e0e0", "#333333"))
        missed_header.grid(row=0, column=0, padx=12, pady=(12, 0), sticky="ew")
        ctk.CTkLabel(
            missed_header,
            text=(
                f"❶ Excel 有但本次沒掃到 ({len(self.missed)} 位) "
                "— 您可手動填本次裝評；留空則略過該位成員。"
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

        # ===== ❷ Fuzzy proposals (OCR saw a similar name) =====
        fuzzy_header = ctk.CTkFrame(self, fg_color=("#e0e0e0", "#333333"))
        fuzzy_header.grid(row=2, column=0, padx=12, pady=(12, 0), sticky="ew")
        ctk.CTkLabel(
            fuzzy_header,
            text=(
                f"❷ 模糊提案 ({len(self.fuzzy_proposals)} 位) "
                "— OCR 辨識到的名字與工作簿不完全一致；"
                "勾「套用」即寫入提案值，或自行手填。"
            ),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        self._fuzzy_frame = ctk.CTkScrollableFrame(
            self, fg_color=("#fafafa", "#1f1f1f"),
            label_text=None,
        )
        self._fuzzy_frame.grid(row=3, column=0, padx=12, pady=0, sticky="nsew")
        self._fuzzy_frame.grid_columnconfigure(0, weight=0)

        self._populate_fuzzy_proposals()

        # ===== ❸ Unmatched (OCR saw it, no Excel match) =====
        unmatched_header = ctk.CTkFrame(self, fg_color=("#e0e0e0", "#333333"))
        unmatched_header.grid(row=4, column=0, padx=12, pady=(12, 0), sticky="ew")
        ctk.CTkLabel(
            unmatched_header,
            text=(
                f"❸ 辨識到但無 Excel 對應 ({len(self.unmatched)} 位) "
                "— 僅供參考，這些不會寫入工作簿。"
            ),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        self._unmatched_frame = ctk.CTkScrollableFrame(
            self, fg_color=("#fafafa", "#1f1f1f"),
            label_text=None,
        )
        self._unmatched_frame.grid(row=5, column=0, padx=12, pady=(0, 8), sticky="nsew")
        self._unmatched_frame.grid_columnconfigure(0, weight=0)

        self._populate_unmatched()

        # ===== Buttons =====
        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=6, column=0, padx=12, pady=(4, 16))
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
        h = ctk.CTkFrame(self._missed_frame, fg_color="transparent")
        h.grid(row=0, column=0, padx=2, pady=(2, 4), sticky="w")
        bold = ctk.CTkFont(size=12, weight="bold")
        ctk.CTkLabel(h, text="ID", width=_M_ID_W, anchor="center", font=bold).grid(
            row=0, column=0, padx=4)
        ctk.CTkLabel(h, text="名字", width=_M_NAME_W, anchor="w", font=bold).grid(
            row=0, column=1, padx=4)
        ctk.CTkLabel(h, text="上次紀錄", width=_M_LAST_W, anchor="w", font=bold).grid(
            row=0, column=2, padx=4)
        ctk.CTkLabel(h, text="模糊提案（套用即寫入）", width=_M_CAND_W, anchor="w", font=bold).grid(
            row=0, column=3, padx=4)
        ctk.CTkLabel(h, text="或手填裝評", width=_M_INPUT_W, anchor="w", font=bold).grid(
            row=0, column=4, padx=4)
        ctk.CTkLabel(h, text="操作", width=_M_RENAME_W, anchor="center", font=bold).grid(
            row=0, column=5, padx=4)

        for r_idx, item in enumerate(self.missed, start=1):
            row = ctk.CTkFrame(self._missed_frame, fg_color=("#ffffff", "#262626"))
            row.grid(row=r_idx, column=0, padx=2, pady=1, sticky="w")

            # ID column
            rec = (self.workbook.records[item.record_index]
                   if self.workbook and 0 <= item.record_index < len(self.workbook.records)
                   else None)
            id_text = str(rec.player_id) if (rec and rec.player_id is not None) else "—"
            ctk.CTkLabel(
                row, text=id_text, width=_M_ID_W, anchor="center",
                text_color=("#666666", "#aaaaaa"),
            ).grid(row=0, column=0, padx=4, pady=4, sticky="w")

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
            entry = ctk.CTkEntry(row, width=_M_INPUT_W, placeholder_text="留空略過")
            if item.candidate_gear is not None:
                apply_var = ctk.BooleanVar(value=False)

                def _on_toggle(*_args, idx=item.record_index, var=apply_var, e=entry):
                    if var.get():
                        e.delete(0, "end")
                        e.configure(state="disabled")
                    else:
                        e.configure(state="normal")

                apply_var.trace_add("write", _on_toggle)
                self._apply_vars[item.record_index] = apply_var
                cand_text = (
                    f"套用: {item.candidate_gear:,}"
                    f"   (OCR:{item.candidate_ocr_name or '?'}"
                    f", {item.candidate_score:.0f}%)"
                ) if item.candidate_score is not None else (
                    f"套用: {item.candidate_gear:,}"
                    f"   (OCR:{item.candidate_ocr_name or '?'})"
                )
                ctk.CTkCheckBox(
                    row,
                    text=cand_text,
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
            rename_btn.grid(row=0, column=5, padx=4, pady=4, sticky="w")
            self._row_widgets[item.record_index] = {
                "name_label": name_label, "rename_btn": rename_btn,
            }

    def _populate_fuzzy_proposals(self) -> None:
        """Render ❷: READ-ONLY visual aid for the fuzzy hits in ❶.

        For every fuzzy proposal already listed in ❶ this section
        shows the source-screenshot row-strip plus the Excel name and
        OCR metadata (gear, similarity %, confidence). No 套用 checkbox
        or 手填 entry is rendered here — those interactive controls
        stay in ❶ so the user makes their decision in one place.
        ❷'s sole purpose is to make it visually obvious whether the
        proposal latched on to the right player.
        """
        if not self.fuzzy_proposals:
            ctk.CTkLabel(
                self._fuzzy_frame, text="（沒有需要確認的模糊提案）",
                text_color=("#888888", "#777777"),
            ).grid(row=0, column=0, padx=8, pady=12, sticky="w")
            return

        for r_idx, item in enumerate(self.fuzzy_proposals):
            card = ctk.CTkFrame(
                self._fuzzy_frame,
                fg_color=("#ffffff", "#262626"),
                border_width=1,
                border_color=("#cccccc", "#444444"),
            )
            card.grid(row=r_idx, column=0, padx=4, pady=6, sticky="w")

            # ---- top: Excel name + OCR-side details (single sub-row) ----
            meta = ctk.CTkFrame(card, fg_color="transparent")
            meta.grid(row=0, column=0, padx=8, pady=(6, 2), sticky="w")

            rec = (
                self.workbook.records[item.record_index]
                if self.workbook and 0 <= item.record_index < len(self.workbook.records)
                else None
            )
            id_text = str(rec.player_id) if (rec and rec.player_id is not None) else "—"
            ctk.CTkLabel(
                meta, text=f"#{id_text}", anchor="center",
                text_color=("#666666", "#aaaaaa"),
            ).grid(row=0, column=0, padx=(0, 6), sticky="w")
            ctk.CTkLabel(
                meta, text=item.name, anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=0, column=1, padx=(0, 12), sticky="w")
            ocr_name = item.candidate_ocr_name or "(空)"
            ctk.CTkLabel(
                meta, text=f"OCR: {ocr_name}", anchor="w",
            ).grid(row=0, column=2, padx=(0, 12), sticky="w")
            ctk.CTkLabel(
                meta, text=f"裝評: {item.candidate_gear:,}", anchor="w",
            ).grid(row=0, column=3, padx=(0, 12), sticky="w")
            if item.candidate_score is not None:
                ctk.CTkLabel(
                    meta,
                    text=f"相似 {item.candidate_score:.0f}%",
                    text_color=("#1d6f3a", "#7cd693"),
                ).grid(row=0, column=4, padx=(0, 12), sticky="w")
            if item.candidate_confidence is not None:
                ctk.CTkLabel(
                    meta,
                    text=f"信心 {item.candidate_confidence:.2f}",
                    text_color=("#666666", "#bbbbbb"),
                ).grid(row=0, column=5, padx=(0, 12), sticky="w")
            if item.candidate_page is not None:
                ctk.CTkLabel(
                    meta,
                    text=f"📄 page_{item.candidate_page:03d}.png",
                    text_color=("#666666", "#bbbbbb"),
                ).grid(row=0, column=6, padx=(0, 0), sticky="w")

            # ---- bottom: row-strip screenshot ---------------------------
            thumb = self._make_row_thumbnail(
                item.candidate_image_path, item.candidate_row_y,
            )
            if thumb is not None:
                ctk.CTkLabel(card, text="", image=thumb).grid(
                    row=1, column=0, padx=8, pady=(2, 8), sticky="w",
                )
            else:
                ctk.CTkLabel(
                    card,
                    text="(找不到原始截圖)",
                    width=_U_THUMB_W,
                    anchor="w",
                    text_color=("#888888", "#777777"),
                ).grid(row=1, column=0, padx=8, pady=(2, 8), sticky="w")

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

    def _populate_unmatched(self) -> None:
        if not self.unmatched:
            ctk.CTkLabel(
                self._unmatched_frame, text="（全部辨識到的人都已配對成功）",
                text_color=("#888888", "#777777"),
            ).grid(row=0, column=0, padx=8, pady=12, sticky="w")
            return

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

            # ---- bottom: row-strip screenshot ----------------------------
            thumb = self._make_row_thumbnail(item.image_path, item.row_y)
            if thumb is not None:
                ctk.CTkLabel(card, text="", image=thumb).grid(
                    row=1, column=0, padx=8, pady=(2, 8), sticky="w",
                )
            else:
                ctk.CTkLabel(
                    card,
                    text="(找不到原始截圖)",
                    width=_U_THUMB_W,
                    anchor="w",
                    text_color=("#888888", "#777777"),
                ).grid(row=1, column=0, padx=8, pady=(2, 8), sticky="w")

    def _make_row_thumbnail(
        self,
        image_path: "Path | None",
        row_y: int | None,
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
                size=(_U_THUMB_W, _U_THUMB_H),
            )
            self._thumbnail_cache.append(ctk_img)
            return ctk_img
        except Exception:
            return None

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

        # If the caller returns False, the workbook write failed (e.g.
        # Excel was holding the file open). Keep the review dialog
        # alive so the user can fix it and click 確認並儲存 again
        # rather than losing all their manual inputs.
        result = self.on_confirm(ReviewDecisions(
            manual_gear=manual_gear, approved_fuzzy=approved_fuzzy,
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
