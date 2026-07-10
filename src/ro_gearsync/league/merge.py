"""Combine the (up to) five league scans, match players to the roster, and
reconcile against the in-game 參戰人數.

Pipeline:

  1. Within each battlefield, its scans (主戰場：輸出/輔助；副戰場：輸出/輔助/
     戰略) show the *same* players; merge their rows by name into one
     :class:`PlayerBattleStats` per player, remembering which screens each
     player was seen on (``source_views``) so the review dialog can group
     findings by screen.
  2. Match each battlefield's players to the league roster, reusing the
     gear matcher — EXACT ONLY (遊戲ID → Last_OCR_ID incl. stored
     variants). Fuzzy auto-matching was removed 2026-07-07 after it
     cross-paired two similar names in a live battle; non-exact rows go
     to manual assignment instead. A fresh matcher per battlefield,
     because one roster member can legitimately play **both** 主 and 副.
  3. Cross-battlefield, fold each roster member's main/sub stats into one
     :class:`MatchedPlayer` carrying a 主/副/主+副 participation marker.
  4. Rows that match nothing become unmatched (red) entries for manual review.
  5. Per SCREEN (not just per battlefield), compare 參戰人數 vs
     rows-recognised vs auto-matched — only screens that were actually
     scanned produce a :class:`Reconciliation` entry.

Names are merged with the gear matcher's normalisation so OCR variants of the
same name (e.g. 輸出 view vs 輔助 view) collapse together.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..matching import build_matcher, normalize_for_match
from ..storage.excel import PlayerRecord
from .model import (
    BATTLEFIELD_LABEL,
    VIEW_LABEL,
    Battlefield,
    LeagueRow,
    PlayerBattleStats,
    Scan,
    View,
)

def screen_label_zh(battlefield: Battlefield, view: View) -> str:
    return f"{BATTLEFIELD_LABEL[battlefield]}戰場・{VIEW_LABEL[view]}"


@dataclass
class MatchedPlayer:
    """One roster member (or one unmatched capture) for the overview sheet."""
    record_index: int | None          # None ⇒ unmatched capture (red row)
    player_id: int | None
    nickname: str                      # 遊戲ID for matched, OCR name for unmatched
    profession: str
    last_ocr: str
    main: PlayerBattleStats | None = None
    sub: PlayerBattleStats | None = None
    # True when this row needs a human eyeball (unmatched capture).
    needs_review: bool = False
    review_note: str = ""

    @property
    def participation(self) -> str:
        marks = []
        if self.main is not None:
            marks.append(BATTLEFIELD_LABEL["main"])
        if self.sub is not None:
            marks.append(BATTLEFIELD_LABEL["sub"])
        return "+".join(marks)   # 主 / 副 / 主+副

    @property
    def is_unmatched(self) -> bool:
        return self.record_index is None

    @property
    def source_screens(self) -> str:
        """「主戰場・輸出、副戰場・戰略」-style origin label for review UIs."""
        parts: list[str] = []
        for bf, stats in (("main", self.main), ("sub", self.sub)):
            if stats is None:
                continue
            for view in sorted(stats.source_views):
                parts.append(screen_label_zh(bf, view))  # type: ignore[arg-type]
        return "、".join(parts)


@dataclass
class Reconciliation:
    """Per-SCREEN tally — one entry per scan the user actually captured."""
    battlefield: Battlefield
    view: View
    participant_count: int | None      # what the game showed on that screen
    recognized: int                    # rows recognised on that screen
    auto_matched: int                  # exact-matched to a roster member
    review: int                        # unmatched rows needing a human
    # Rows remaining after the same-name merge for this screen. Should
    # equal ``recognized``; fewer means two recognised rows folded into
    # one — an anomaly worth a human eyeball (the review dialog flags it).
    merged: int = 0

    @property
    def label(self) -> str:
        return screen_label_zh(self.battlefield, self.view)

    @property
    def delta(self) -> int | None:
        if self.participant_count is None:
            return None
        return self.recognized - self.participant_count


@dataclass
class BattleResult:
    players: list[MatchedPlayer] = field(default_factory=list)
    reconciliations: list[Reconciliation] = field(default_factory=list)

    @property
    def participants(self) -> list[MatchedPlayer]:
        return [p for p in self.players if p.participation]

    @property
    def review_players(self) -> list[MatchedPlayer]:
        return [p for p in self.players if p.needs_review]


# --------------------------------------------------------------------- internals


def _merge_views(scans: list[Scan]) -> dict[str, PlayerBattleStats]:
    """Merge one battlefield's scans into name → stats.

    Rows are keyed by normalised name so the views' spellings collapse.
    The first-seen original name is kept for display; ``source_views``
    records every screen the player was seen on.

    Same-name COLLISION guard (2026-07-08): within one view every player
    appears exactly once (cross-page duplicates are already deduped), so
    two rows that share a name but disagree on an already-filled metric
    cannot be one person — they're twin names (岡本002/003) that OCR read
    as the same string. ``_final_collapse`` deliberately keeps such rows
    apart; folding them here by name would overwrite one player's numbers
    with the other's. On conflict the key is disambiguated ("name#2", …)
    so both survive to roster matching — the second one exact-matches
    nothing and lands in manual assignment, as designed. Cross-view
    merging is unaffected: one battlefield's views share no metric
    columns, so they can never conflict.
    """
    by_key: dict[str, PlayerBattleStats] = {}
    bf = scans[0].battlefield if scans else "main"
    for scan in scans:
        for row in scan.rows:
            base = normalize_for_match(row.name) or row.name
            key = base
            serial = 1
            stats = by_key.get(key)
            while stats is not None and _row_conflicts(stats, row):
                serial += 1
                key = f"{base}#{serial}"
                stats = by_key.get(key)
            if stats is None:
                stats = PlayerBattleStats(name=row.name, battlefield=bf)
                by_key[key] = stats
            stats.source_views.add(scan.view)
            for k, v in row.metrics.items():
                stats.set_metric(k, v)
    return by_key


def _row_conflicts(stats: PlayerBattleStats, row: LeagueRow) -> bool:
    """Would folding ``row`` into ``stats`` overwrite different numbers?

    True when the two carry DIFFERENT non-None values for at least one
    shared metric. Views of one battlefield hold disjoint metric sets, so
    a conflict can only arise between two rows of the SAME view — which,
    post-dedup, means two distinct real players whose names OCR collapsed
    into one string.
    """
    for k, v in row.metrics.items():
        if v is None:
            continue
        current = getattr(stats, k)
        if current is not None and current != v:
            return True
    return False


def _match_battlefield(
    stats_by_key: dict[str, PlayerBattleStats],
    roster: list[PlayerRecord],
) -> tuple[dict[int, PlayerBattleStats], list[PlayerBattleStats]]:
    """Match a battlefield's players to roster records — EXACT ONLY.

    Fuzzy auto-matching was dropped on 2026-07-07: in a real battle two
    similar names (Da77 / Da烏佰爵) fuzzy-matched to each other's roster
    rows CROSSED (both at score 67, claim order decided). Wrong-but-
    confident pairings are worse than none, so every non-exact row now
    goes to manual assignment in the review dialog. The dropdown
    assignment writes the OCR string back to Last_OCR_ID, so each manual
    fix is one-time — next battle the same string exact-matches.

    Returns ``(by_record_index, unmatched_stats)``.
    """
    matcher = build_matcher(roster)
    by_record: dict[int, PlayerBattleStats] = {}
    unmatched: list[PlayerBattleStats] = []
    for stats in stats_by_key.values():
        idx = matcher.exact_match_correct(stats.name)
        if idx is None:
            idx = matcher.exact_match_ocr(stats.name)
        if idx is not None:
            matcher.mark_claimed(idx)
            stats.record_index = idx
            stats.match_score = 100.0
            by_record[idx] = stats
            continue
        unmatched.append(stats)
    return by_record, unmatched


def _per_screen_reconciliations(
    bf_scans: list[Scan],
    stats_by_key: dict[str, PlayerBattleStats],
) -> list[Reconciliation]:
    """Tally each captured screen separately (the user reviews per screen).

    Counts walk the merged stats (via ``source_views``) rather than the
    raw rows — with disambiguated collision keys a by-name lookup could
    land on the wrong twin. ``merged`` vs ``recognized`` doubles as the
    final safety net: fewer merged rows than recognised rows means the
    merge folded two rows together, which the dialog paints red.
    """
    out: list[Reconciliation] = []
    for scan in bf_scans:
        screen_stats = [
            s for s in stats_by_key.values() if scan.view in s.source_views
        ]
        auto = sum(
            1 for s in screen_stats
            if s.record_index is not None
            and s.match_score is not None and s.match_score >= 100.0
        )
        out.append(Reconciliation(
            battlefield=scan.battlefield,
            view=scan.view,
            participant_count=scan.participant_count,
            recognized=len(scan.rows),
            auto_matched=auto,
            review=len(screen_stats) - auto,
            merged=len(screen_stats),
        ))
    return out


# --------------------------------------------------------------------- public


def merge_battle(scans: list[Scan], roster: list[PlayerRecord]) -> BattleResult:
    """Fold scans + roster into a :class:`BattleResult`.

    ``scans`` may hold 1–5 entries (主戰場×輸出/輔助＋副戰場×輸出/輔助/戰略；
    the user can scan only some screens). Each (battlefield, view) should
    appear at most once; if one is missing, its metrics simply stay absent
    and it produces no reconciliation entry.
    """
    by_bf: dict[Battlefield, list[Scan]] = {"main": [], "sub": []}
    for s in scans:
        by_bf.setdefault(s.battlefield, []).append(s)

    # Per-battlefield merge + match.
    record_main: dict[int, PlayerBattleStats] = {}
    record_sub: dict[int, PlayerBattleStats] = {}
    unmatched_rows: list[tuple[Battlefield, PlayerBattleStats]] = []
    recon: list[Reconciliation] = []

    for bf in ("main", "sub"):
        bf_scans = by_bf.get(bf, [])
        if not bf_scans:
            continue
        stats_by_key = _merge_views(bf_scans)
        by_record, unmatched = _match_battlefield(stats_by_key, roster)
        (record_main if bf == "main" else record_sub).update(by_record)
        for st in unmatched:
            unmatched_rows.append((bf, st))
        recon.extend(_per_screen_reconciliations(bf_scans, stats_by_key))

    # Fold into per-roster-member rows, preserving roster order.
    players: list[MatchedPlayer] = []
    for i, rec in enumerate(roster):
        main = record_main.get(i)
        sub = record_sub.get(i)
        if main is None and sub is None:
            # Member didn't play this battle — still listed (blank 參與).
            players.append(MatchedPlayer(
                record_index=i, player_id=rec.player_id,
                nickname=rec.correct_nickname or rec.latest_ocr_nickname,
                profession=rec.profession, last_ocr=rec.latest_ocr_nickname,
            ))
            continue
        players.append(MatchedPlayer(
            record_index=i, player_id=rec.player_id,
            nickname=rec.correct_nickname or rec.latest_ocr_nickname,
            profession=rec.profession, last_ocr=rec.latest_ocr_nickname,
            main=main, sub=sub,
        ))

    # Unmatched captures appended as red rows for manual handling.
    for bf, st in unmatched_rows:
        players.append(MatchedPlayer(
            record_index=None, player_id=None,
            nickname=st.name, profession="", last_ocr=st.name,
            main=st if bf == "main" else None,
            sub=st if bf == "sub" else None,
            needs_review=True,
            review_note=f"{BATTLEFIELD_LABEL[bf]}戰場掃到但對不到名冊，請人工指認",
        ))

    return BattleResult(players=players, reconciliations=recon)
