"""Review dialog for the Google-Sheet roster sync (工具 → 從雲端名冊同步).

One card per changed 編號; nothing is written until the user confirms
each card individually (per the 2026-07-08 spec):

  * join / leave / prof cards carry an opt-in checkbox (default OFF);
  * "changed" cards (sheet name ≠ workbook name) force a three-way call
    the program cannot make itself — 換人 (slot changed hands: wipe the
    previous member's data INCLUDING gear-score history) vs 改名 (same
    person: keep history, drop stale Last_OCR_ID) vs 略過;
  * peak updates (sheet 裝備評分 > workbook 最高裝評, names already
    matching, raise-only) share ONE opt-in checkbox — they are
    non-destructive and typically numerous.

套用 asks for one final confirmation, backs both workbooks up, writes
in place, reports what happened, then closes.
"""
from __future__ import annotations

from typing import Callable

import customtkinter as ctk
from tkinter import messagebox

from ..roster_sync.apply import ApplyError, apply_plan
from ..roster_sync.diff import (
    DECISION_APPLY,
    DECISION_RENAME,
    DECISION_REPLACE,
    DECISION_SKIP,
    KIND_CHANGED,
    KIND_LABEL,
    SyncPlan,
)

_RED = "#cc3333"
_GREEN = "#1d8a3d"
_GRAY = "#8a8a8a"


class RosterSyncDialog(ctk.CTkToplevel):
    """``on_applied()`` fires after a successful write (the caller can
    refresh whatever it keeps in memory)."""

    def __init__(
        self,
        master,
        plan: SyncPlan,
        *,
        on_applied: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("雲端名冊同步 — 變更確認")
        self.geometry("720x640")
        self.minsize(600, 480)
        self.transient(master)
        self.grab_set()

        self._plan = plan
        self._on_applied = on_applied
        # member_id → ("check", BooleanVar) | ("choice", StringVar)
        self._controls: dict[int, tuple[str, object]] = {}
        self._peak_var = ctk.BooleanVar(value=False)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_summary()
        self._build_cards()
        self._build_stale_notice()
        self._build_buttons()

    # ------------------------------------------------------------ layout

    def _build_summary(self) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        peak_note = (
            f"、{len(self._plan.peak_updates)} 筆最高裝評更新"
            if self._plan.peak_updates else ""
        )
        ctk.CTkLabel(
            frame,
            text=(
                f"試算表成員 {self._plan.sheet_member_count} 人；"
                f"共 {len(self._plan.changes)} 筆變更{peak_note}待確認"
                "（未勾選／未選擇的項目不會寫入）"
            ),
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w", justify="left",
        ).pack(anchor="w", padx=12, pady=(8, 2))
        for w in self._plan.warnings:
            ctk.CTkLabel(
                frame, text=f"⚠ {w}", text_color=_RED,
                anchor="w", justify="left", wraplength=650,
            ).pack(anchor="w", padx=12, pady=(0, 2))
        ctk.CTkLabel(frame, text="").pack(pady=(0, 4))

    def _build_cards(self) -> None:
        body = ctk.CTkScrollableFrame(self)
        body.grid(row=1, column=0, padx=12, pady=6, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        for i, change in enumerate(self._plan.changes):
            card = ctk.CTkFrame(body)
            card.grid(row=i, column=0, pady=4, padx=4, sticky="ew")
            card.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                card,
                text=f"ID {change.member_id}｜{KIND_LABEL[change.kind]}",
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=10, pady=(8, 0), sticky="w")
            ctk.CTkLabel(
                card, text=change.summary(),
                anchor="w", justify="left", wraplength=620,
            ).grid(row=1, column=0, padx=10, sticky="w")
            for j, note in enumerate(change.notes):
                ctk.CTkLabel(
                    card, text=f"⚠ {note}", text_color=_RED,
                    anchor="w", justify="left", wraplength=620,
                ).grid(row=2 + j, column=0, padx=10, sticky="w")

            ctl_row = 2 + len(change.notes)
            if change.kind == KIND_CHANGED:
                var = ctk.StringVar(value=DECISION_SKIP)
                self._controls[change.member_id] = ("choice", var)
                box = ctk.CTkFrame(card, fg_color="transparent")
                box.grid(row=ctl_row, column=0, padx=10, pady=(4, 8), sticky="w")
                ctk.CTkRadioButton(
                    box, text="換人（清空舊資料，含裝評紀錄）",
                    variable=var, value=DECISION_REPLACE,
                ).pack(side="left", padx=(0, 12))
                ctk.CTkRadioButton(
                    box, text="同一人改名/換職業（保留裝評紀錄）",
                    variable=var, value=DECISION_RENAME,
                ).pack(side="left", padx=(0, 12))
                ctk.CTkRadioButton(
                    box, text="略過", variable=var, value=DECISION_SKIP,
                ).pack(side="left")
            else:
                var = ctk.BooleanVar(value=False)
                self._controls[change.member_id] = ("check", var)
                ctk.CTkCheckBox(
                    card, text="確認套用這筆變更", variable=var,
                ).grid(row=ctl_row, column=0, padx=10, pady=(4, 8), sticky="w")

        if self._plan.peak_updates:
            self._build_peak_card(body, row=len(self._plan.changes))

    def _build_peak_card(self, body, row: int) -> None:
        """One collective card for every raise-only 最高裝評 update —
        a per-member checkbox would drown the real (destructive) cards."""
        card = ctk.CTkFrame(body)
        card.grid(row=row, column=0, pady=4, padx=4, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        updates = self._plan.peak_updates
        ctk.CTkLabel(
            card,
            text=f"最高裝評更新（表單裝備評分較高，共 {len(updates)} 筆，只升不降）",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=10, pady=(8, 0), sticky="w")
        lines = "\n".join(
            f"ID {pu.member_id}｜{pu.summary()}" for pu in updates
        )
        ctk.CTkLabel(
            card, text=lines, anchor="w", justify="left", wraplength=620,
        ).grid(row=1, column=0, padx=10, sticky="w")
        ctk.CTkCheckBox(
            card,
            text="更新以上成員的最高裝評（只寫 guild_scores）",
            variable=self._peak_var,
        ).grid(row=2, column=0, padx=10, pady=(4, 8), sticky="w")

    def _build_stale_notice(self) -> None:
        """Local 最高裝評 beats the sheet for N members — the tool never
        writes to the sheet, so nag the user to update the cloud roster."""
        if not self._plan.sheet_stale_peaks:
            return
        frame = ctk.CTkFrame(self)
        frame.grid(row=2, column=0, padx=12, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(
            frame,
            text=(
                f"⚠ 有 {self._plan.sheet_stale_peaks} 筆成員的本地最高裝評"
                "高於表單上的裝備評分（或表單空白）——"
                "記得將掃描完的裝備評分更新至雲端名冊！"
            ),
            text_color=_RED,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", justify="left", wraplength=650,
        ).pack(anchor="w", padx=12, pady=8)

    def _build_buttons(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=3, column=0, padx=12, pady=(6, 12), sticky="ew")
        ctk.CTkButton(
            bar, text="勾選全部（不含換人/改名）", width=200,
            command=self._check_all,
        ).pack(side="left")
        self._apply_btn = ctk.CTkButton(
            bar, text="套用已確認項目", width=160, command=self._apply,
        )
        self._apply_btn.pack(side="right")
        ctk.CTkButton(
            bar, text="關閉（不寫入）", width=120,
            fg_color="gray40", hover_color="gray30", command=self.destroy,
        ).pack(side="right", padx=(0, 8))

    # ------------------------------------------------------------ actions

    def _check_all(self) -> None:
        # Deliberately leaves the ambiguous 換人/改名 radios untouched —
        # those must stay an explicit human call.
        for kind, var in self._controls.values():
            if kind == "check":
                var.set(True)  # type: ignore[union-attr]
        if self._plan.peak_updates:
            self._peak_var.set(True)

    def _decisions(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for mid, (kind, var) in self._controls.items():
            if kind == "check":
                out[mid] = DECISION_APPLY if var.get() else DECISION_SKIP  # type: ignore[union-attr]
            else:
                out[mid] = var.get()  # type: ignore[union-attr]
        return out

    def _apply(self) -> None:
        decisions = self._decisions()
        sync_peaks = bool(self._plan.peak_updates) and self._peak_var.get()
        chosen = sum(1 for d in decisions.values() if d != DECISION_SKIP)
        if not chosen and not sync_peaks:
            messagebox.showinfo(
                "沒有已確認的項目",
                "請先勾選要套用的變更（或為「換人/改名」項目做出選擇）。",
                parent=self,
            )
            return
        peak_part = (
            f"、{len(self._plan.peak_updates)} 筆最高裝評更新" if sync_peaks else ""
        )
        if not messagebox.askyesno(
            "確認寫入",
            f"將套用 {chosen} 筆變更{peak_part}到 guild_scores 與 league_scores，"
            "寫入前會自動備份兩份檔案。\n\n確定要寫入嗎？",
            parent=self,
        ):
            return

        self._apply_btn.configure(state="disabled")
        try:
            report = apply_plan(self._plan, decisions, sync_peaks=sync_peaks)
        except ApplyError as exc:
            self._apply_btn.configure(state="normal")
            messagebox.showerror("寫入失敗", str(exc), parent=self)
            return

        lines = "\n".join(report.applied)
        if report.peak_applied:
            # Cap the messagebox — 100+ peak lines would blow past the
            # screen; the full list was visible on the card already.
            shown = report.peak_applied[:12]
            more = len(report.peak_applied) - len(shown)
            peak_lines = "\n".join(shown) + (f"\n…等共 {len(report.peak_applied)} 筆" if more > 0 else "")
            lines = (lines + "\n\n" if lines else "") + f"最高裝評更新：\n{peak_lines}"
        backups = "\n".join(str(b) for b in report.backups)
        messagebox.showinfo(
            "同步完成",
            f"已套用 {report.applied_count} 筆、略過 {report.skipped} 筆"
            f"{f'、最高裝評更新 {len(report.peak_applied)} 筆' if report.peak_applied else ''}。\n\n"
            f"{lines}\n\n備份：\n{backups}",
            parent=self,
        )
        if self._on_applied:
            self._on_applied()
        self.destroy()
