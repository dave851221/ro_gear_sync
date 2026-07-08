"""Data models for the league (公會聯賽) capture feature.

A full league *battle* is captured as up to **5 scans**:

    主戰場 × 輸出 (main/dps)      主戰場 × 輔助 (main/support)
    副戰場 × 輸出 (sub/dps)       副戰場 × 輔助 (sub/support)
    副戰場 × 戰略 (sub/strategy)

The views of one battlefield show the *same* players with different
metric columns, so they merge by player name into a single
:class:`PlayerBattleStats` per (battlefield, player). Participation in each
battlefield then drives the 主/副 marker on the overview sheet.

Terminology kept English in code; Chinese labels live in the LABELS map for
the Excel writer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Battlefield = Literal["main", "sub"]           # 主戰場 / 副戰場
View = Literal["dps", "support", "strategy"]   # 輸出 / 輔助 / 戰略

# Metric keys per view, in display (left-to-right) order. The 戰略 view
# exists only on the sub battlefield (captured live 2026-07-02).
DPS_METRICS: tuple[str, ...] = ("kills", "assists", "player_damage", "building_damage")
SUPPORT_METRICS: tuple[str, ...] = ("heal", "damage_taken", "deaths", "revives")
STRATEGY_METRICS: tuple[str, ...] = ("minions", "flag_repairs", "boss_last_hits", "boss_damage")
ALL_METRICS: tuple[str, ...] = DPS_METRICS + SUPPORT_METRICS + STRATEGY_METRICS

VIEW_METRICS: dict[str, tuple[str, ...]] = {
    "dps": DPS_METRICS,
    "support": SUPPORT_METRICS,
    "strategy": STRATEGY_METRICS,
}

# English key → Chinese column header (Excel).
LABELS: dict[str, str] = {
    "kills": "擊殺",
    "assists": "助攻",
    "player_damage": "玩家傷害",
    "building_damage": "建築傷害",
    "heal": "治療",
    "damage_taken": "承傷",
    "deaths": "死亡",
    "revives": "復活",
    "minions": "小怪",
    "flag_repairs": "修旗",
    "boss_last_hits": "王的最後一擊",
    "boss_damage": "王的傷害",
}

BATTLEFIELD_LABEL: dict[str, str] = {"main": "主", "sub": "副"}
VIEW_LABEL: dict[str, str] = {"dps": "輸出", "support": "輔助", "strategy": "戰略"}


@dataclass
class LeagueRow:
    """One player row recognised from a single screenshot (one view).

    ``metrics`` holds only the keys relevant to the scan's view (the four
    DPS keys or the four support keys). Missing/unreadable cells are stored
    as ``None`` so the caller can decide to treat them as 0.
    """
    name: str
    metrics: dict[str, int | None] = field(default_factory=dict)


@dataclass
class Scan:
    """One (battlefield, view) scan, already deduped across scrolled pages."""
    battlefield: Battlefield
    view: View
    participant_count: int | None
    rows: list[LeagueRow] = field(default_factory=list)

    @property
    def metric_keys(self) -> tuple[str, ...]:
        return VIEW_METRICS[self.view]


@dataclass
class PlayerBattleStats:
    """One player's merged stats within ONE battlefield (dps + support).

    All eight metrics live here; the ones not yet filled stay ``None``.
    ``name`` is the recognised OCR name (best seen). ``record_index`` /
    ``player_id`` are populated by the roster-matching step.
    """
    name: str
    battlefield: Battlefield
    kills: int | None = None
    assists: int | None = None
    player_damage: int | None = None
    building_damage: int | None = None
    heal: int | None = None
    damage_taken: int | None = None
    deaths: int | None = None
    revives: int | None = None
    # 戰略 view (sub battlefield only).
    minions: int | None = None
    flag_repairs: int | None = None
    boss_last_hits: int | None = None
    boss_damage: int | None = None

    # Filled by the matcher (None until matched / for unmatched rows).
    record_index: int | None = None
    player_id: int | None = None
    # 100.0 = trusted identity (exact match on 遊戲ID / Last_OCR_ID, or
    # human-confirmed in the review dialog); None = not matched. Drives
    # the per-screen 自動對應 tally in the reconciliation summary.
    match_score: float | None = None
    # Which screens (views) this player was seen on within the battlefield —
    # lets the review dialog say which scan a finding came from.
    source_views: set = field(default_factory=set)
    # OCR spelling variants absorbed into this stats object (e.g. the 輔助
    # screen misread the name differently and the user assigned it here).
    # All of them get written back to the roster's multi-value Last_OCR_ID
    # so next battle exact-matches every observed spelling.
    alt_names: set = field(default_factory=set)

    def set_metric(self, key: str, value: int | None) -> None:
        if value is not None or getattr(self, key) is None:
            setattr(self, key, value)

    def as_metric_dict(self) -> dict[str, int | None]:
        return {k: getattr(self, k) for k in ALL_METRICS}
