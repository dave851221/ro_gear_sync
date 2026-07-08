"""Post-scan review dialog for the 聯賽評分 tab.

Organised **per screen** (主戰場×輸出/輔助＋副戰場×輸出/輔助/戰略) so the
user knows exactly which in-game page to open when double-checking:

  ❶ 對帳摘要 — one line per scanned screen: 參戰人數 vs 辨識 vs 自動對應.
     Anything needing human eyes is painted red.
  ❷ 對不到名冊 — grouped by source screen. A dropdown (ID-sorted, wheel-
     scrollable when open, listing ONLY members without data for that
     row's screen(s)) assigns the name to a member, or keeps it as a red
     row. Duplicate assignments are rejected on confirm.

  Fuzzy auto-matching was removed 2026-07-07 (it cross-paired two similar
  names in a live battle) — every non-exact row lands in ❷, and the
  manual assignment writes Last_OCR_ID back so next battle exact-matches.

Only screens that were actually scanned appear. The dialog stays open
after 確認寫入 — every press writes a *fresh* timestamped snapshot using
the current selections, so the user can adjust and re-write.

The dialog mutates nothing itself — it hands a decision dict to the
caller; decisions reference players by their index in ``result.players``
(stable across the deep-copies the caller makes per write).
"""
from __future__ import annotations

from typing import Callable

import customtkinter as ctk
from tkinter import messagebox, ttk

from ..league.merge import BattleResult, MatchedPlayer, screen_label_zh

KEEP_RED = "（保留為紅列，稍後自行處理）"
_RED = "#cc3333"
_GREEN = "#1d8a3d"

# Screen display order (mirrors the capture buttons).
_SCREEN_ORDER: list[tuple[str, str]] = [
    ("main", "dps"), ("main", "support"),
    ("sub", "dps"), ("sub", "support"), ("sub", "strategy"),
]


def _primary_screen(p: MatchedPlayer) -> tuple[str, str] | None:
    """First screen (in display order) this player's data came from."""
    for bf, view in _SCREEN_ORDER:
        stats = p.main if bf == "main" else p.sub
        if stats is not None and view in stats.source_views:
            return bf, view
    return None


class LeagueReviewDialog(ctk.CTkToplevel):
    """Review + write dialog. ``on_confirm(decisions)`` performs one write
    and returns the written path (or None on failure); the dialog stays
    open so the user can adjust selections and write again."""

    def __init__(
        self,
        master,
        result: BattleResult,
        *,
        on_confirm: Callable[[dict], object],
        on_closed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("聯賽掃描結果確認")
        self.geometry("760x680")
        self.minsize(640, 500)
        self.transient(master)
        self.grab_set()

        self._result = result
        self._on_confirm = on_confirm
        # Fired once when the dialog goes away, with "did at least one
        # write succeed". The panel uses it to decide whether this
        # battle's scans are consumed (reset) or should stay around for
        # another 產出結果 attempt.
        self._on_closed = on_closed
        self._wrote_any = False
        self.protocol("WM_DELETE_WINDOW", self._close)
        # player-list index → (StringVar, {label: record_index}).
        self._assign_vars: dict[int, tuple[ctk.StringVar, dict[str, int]]] = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_summary(result)
        self._build_body(result)
        self._build_buttons()

    # ------------------------------------------------------------ sections

    def _build_summary(self, result: BattleResult) -> None:
        summary = ctk.CTkFrame(self)
        summary.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        ctk.CTkLabel(
            summary, text="❶ 對帳摘要（依掃描畫面）",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))
        for r in result.reconciliations:
            problems: list[str] = []
            if r.delta is not None and r.delta != 0:
                problems.append(f"人數差 {r.delta:+d}")
            if r.review:
                problems.append(f"{r.review} 筆待人工確認")
            # Merge anomaly: two recognised rows folded into one — should
            # never happen post-dedup, so flag it for a human eyeball.
            if r.merged and r.merged != r.recognized:
                problems.append(f"合併異常（辨識 {r.recognized} 列 → 合併後 {r.merged} 列）")
            suffix = f"　⚠ {'、'.join(problems)}" if problems else "　✓"
            ctk.CTkLabel(
                summary,
                text=(
                    f"{r.label}：參戰人數 {r.participant_count or '?'}／"
                    f"辨識 {r.recognized}／自動對應 {r.auto_matched}{suffix}"
                ),
                text_color=_RED if problems else _GREEN,
                anchor="w",
            ).pack(anchor="w", padx=20, pady=1)
        ctk.CTkLabel(summary, text="").pack(pady=(0, 4))

    def _build_body(self, result: BattleResult) -> None:
        body = ctk.CTkScrollableFrame(self)
        body.grid(row=1, column=0, padx=12, pady=6, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self._row = 0

        def _line(widget) -> None:
            widget.grid(row=self._row, column=0, padx=8, pady=2, sticky="ew")
            self._row += 1

        # Group review items by their primary source screen; only screens
        # that were scanned (= appear in reconciliations) are listed.
        # (自動模糊比對已於 2026-07-07 廢除——非精確命中一律進 ❷ 人工指認。)
        scanned = [(r.battlefield, r.view) for r in result.reconciliations]
        unmatched = [p for p in result.players if p.is_unmatched]

        by_screen_unmatched: dict[tuple, list[MatchedPlayer]] = {}
        for p in unmatched:
            by_screen_unmatched.setdefault(_primary_screen(p) or ("?", "?"), []).append(p)

        # Assignment candidates, ID-sorted. Per the 2026-07-07 refinement the
        # list is filtered PER ROW: only members who don't yet have data for
        # the screen(s) the source row carries (view granularity — a member
        # with 輸出 data but no 輔助 data IS offered for a 輔助-sourced row).
        self._members = sorted(
            (p for p in result.players
             if p.record_index is not None and p.player_id is not None
             and p.nickname.strip()),   # skip placeholder slots (ID, no 遊戲ID)
            key=lambda p: p.player_id,
        )

        def _candidates_for(src: MatchedPlayer) -> dict[str, int]:
            out: dict[str, int] = {}
            for m in self._members:
                clash = False
                for bf in ("main", "sub"):
                    s_stats = getattr(src, bf)
                    m_stats = getattr(m, bf)
                    if (
                        s_stats is not None and m_stats is not None
                        and (s_stats.source_views & m_stats.source_views)
                    ):
                        clash = True
                        break
                if not clash:
                    out[f"{m.player_id}｜{m.nickname}"] = m.record_index
            return out

        header_font = ctk.CTkFont(size=14, weight="bold")
        sub_font = ctk.CTkFont(size=13, weight="bold")

        _line(ctk.CTkLabel(
            body,
            text=f"❷ 對不到名冊（{len(unmatched)}）— 可指定成員"
                 "（僅列出該畫面尚無資料者），或保留紅列",
            font=header_font, anchor="w",
            text_color=_RED if unmatched else None,
        ))
        if not unmatched:
            _line(ctk.CTkLabel(body, text="（無）", anchor="w"))
        player_index = {id(p): i for i, p in enumerate(result.players)}
        for screen in scanned:
            items = by_screen_unmatched.get(screen, [])
            if not items:
                continue
            _line(ctk.CTkLabel(
                body, text=f"◤ {screen_label_zh(*screen)}",
                font=sub_font, anchor="w",
            ))
            for p in items:
                frame = ctk.CTkFrame(body, fg_color="transparent")
                frame.grid(row=self._row, column=0, padx=24, pady=2, sticky="ew")
                self._row += 1
                ctk.CTkLabel(
                    frame, text=f"「{p.nickname}」 [{p.participation}] →",
                    anchor="w", text_color=_RED,
                ).grid(row=0, column=0, padx=(0, 6))
                var = ctk.StringVar(value=KEEP_RED)
                candidates = _candidates_for(p)
                # ttk.Combobox instead of CTkOptionMenu: its popdown is a
                # native Listbox, so a 140+-entry roster scrolls with the
                # MOUSE WHEEL (tk menus only offer the tiny arrow buttons).
                combo = ttk.Combobox(
                    frame, textvariable=var, values=[KEEP_RED, *candidates],
                    state="readonly", width=32, height=18,
                )
                combo.grid(row=0, column=1, ipady=2)
                # Wheel must scroll ONLY inside the opened popdown. A closed
                # readonly combobox on Windows cycles values on wheel —
                # scrolling the dialog could silently change an assignment.
                # 'break' stops the class binding; the popdown is a separate
                # toplevel Listbox, so its wheel scrolling is unaffected.
                combo.bind("<MouseWheel>", lambda e: "break")
                self._assign_vars[player_index[id(p)]] = (var, candidates)

    def _build_buttons(self) -> None:
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, padx=12, pady=(6, 12), sticky="ew")
        buttons.grid_columnconfigure(0, weight=1)
        self.written_label = ctk.CTkLabel(buttons, text="", anchor="w")
        self.written_label.grid(row=0, column=0, sticky="w", padx=4)
        ctk.CTkButton(
            buttons, text="關閉", width=100, fg_color="#6e7681",
            hover_color="#57606a", command=self._close,
        ).grid(row=0, column=1, padx=4)
        ctk.CTkButton(
            buttons, text="✅ 確認寫入 Excel", width=180,
            fg_color="#2ea043", hover_color="#238636",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._confirm,
        ).grid(row=0, column=2, padx=4)

    # ----------------------------------------------------------- confirm

    def _confirm(self) -> None:
        assigns: dict[int, int | None] = {}
        for pidx, (var, candidates) in self._assign_vars.items():
            assigns[pidx] = candidates.get(var.get())

        # Guard: two names assigned to the SAME roster member would make
        # the later one silently overwrite the earlier one's stats.
        chosen = [idx for idx in assigns.values() if idx is not None]
        if len(chosen) != len(set(chosen)):
            dup_names = [
                p.nickname for p in self._result.players
                if p.record_index in {i for i in chosen if chosen.count(i) > 1}
            ]
            messagebox.showerror(
                "重複指派",
                "有兩個以上「對不到名冊」的名字被指定給同一位成員：\n"
                f"{'、'.join(dup_names)}\n請修正後再寫入。",
                parent=self,
            )
            return

        # Guard: only a real per-SCREEN overlap counts as a conflict.
        # A member auto-recognised on 主戰場・輸出 but missing 輔助 data is
        # NOT "already recognised" for a 輔助-sourced assignment — that
        # assignment fills the gap (2026-07-07 refinement). We warn only
        # when the source row carries data for a view the target already
        # has, because writing would then overwrite those numbers.
        by_record = {
            p.record_index: p for p in self._result.players
            if p.record_index is not None
        }
        conflicts: list[str] = []
        for pidx, idx in assigns.items():
            if idx is None:
                continue
            src = self._result.players[pidx]
            target = by_record.get(idx)
            if target is None:
                continue
            for bf in ("main", "sub"):
                s_stats = getattr(src, bf)
                t_stats = getattr(target, bf)
                if s_stats is None or t_stats is None:
                    continue
                for view in sorted(s_stats.source_views & t_stats.source_views):
                    conflicts.append(
                        f"「{src.nickname}」→「{target.nickname}」"
                        f"（{screen_label_zh(bf, view)} 已有資料）"
                    )
        if conflicts:
            if not messagebox.askyesno(
                "已存在資料",
                "以下指派的成員在該畫面已有自動辨識到的結果：\n"
                + "\n".join(conflicts)
                + "\n\n已存在資料，將覆蓋自動辨識到的結果。確定要寫入嗎？",
                parent=self,
            ):
                return

        decisions = {
            # player-list index → record_index or None
            "assign": assigns,
        }
        # The dialog STAYS OPEN — each press writes a fresh timestamped
        # snapshot with the current selections.
        out = self._on_confirm(decisions)
        if out:
            self._wrote_any = True
            self.written_label.configure(
                text=f"✅ 已寫入 {getattr(out, 'name', out)}（可調整選項後再次寫入）",
                text_color=_GREEN,
            )

    def _close(self) -> None:
        on_closed = self._on_closed
        wrote_any = self._wrote_any
        self.destroy()
        if on_closed is not None:
            on_closed(wrote_any)


def apply_review_decisions(result: BattleResult, decisions: dict) -> None:
    """Mutate ``result`` according to the dialog's decisions.

    Assigned unmatched rows get their stats attached to the chosen roster
    member, marked human-confirmed (score 100). (Fuzzy approval decisions
    were removed along with fuzzy auto-matching on 2026-07-07 — the only
    decision left is the manual assignment dropdown.)

    Callers should pass a fresh (deep-copied) ``result`` per invocation —
    decisions reference players by list index / record index, both stable
    across ``copy.deepcopy``.
    """
    by_record = {
        p.record_index: p for p in result.players if p.record_index is not None
    }

    to_remove: list[MatchedPlayer] = []
    for pidx, idx in decisions.get("assign", {}).items():
        if idx is None:
            continue
        if not 0 <= pidx < len(result.players):
            continue
        src = result.players[pidx]
        target = by_record.get(idx)
        if target is None or not src.is_unmatched:
            continue
        for field_name in ("main", "sub"):
            stats = getattr(src, field_name)
            if stats is None:
                continue
            stats.record_index = idx
            stats.match_score = 100.0  # human-confirmed
            existing = getattr(target, field_name)
            if existing is None:
                setattr(target, field_name, stats)
                continue
            # Target already holds data for this battlefield (e.g. its 輸出
            # numbers auto-matched while the 輔助 spelling didn't) — MERGE
            # per metric instead of replacing, or we'd silently drop the
            # views the target already has. On overlapping views the
            # human-confirmed source wins.
            for k, v in stats.as_metric_dict().items():
                if v is not None:
                    setattr(existing, k, v)
            existing.source_views |= stats.source_views
            existing.match_score = 100.0
            # Remember the source's spelling as a variant — the roster
            # write-back stores it in Last_OCR_ID so NEXT battle this
            # misread exact-matches instead of needing manual assignment
            # again.
            if stats.name and stats.name != existing.name:
                existing.alt_names.add(stats.name)
            existing.alt_names |= stats.alt_names
        target.needs_review = False
        target.review_note = ""
        to_remove.append(src)
    for src in to_remove:
        result.players.remove(src)
