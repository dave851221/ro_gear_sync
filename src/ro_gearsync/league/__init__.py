"""公會聯賽戰績擷取 — separate snapshot workbook, Gemini-recognised.

Kept deliberately decoupled from the gear-score capture so neither feature
leaks complexity into the other. See ``PLAN_LEAGUE.md`` for the design.
"""
from __future__ import annotations

from .model import (
    ALL_METRICS,
    BATTLEFIELD_LABEL,
    DPS_METRICS,
    LABELS,
    SUPPORT_METRICS,
    VIEW_LABEL,
    VIEW_METRICS,
    Battlefield,
    LeagueRow,
    PlayerBattleStats,
    Scan,
    View,
)
from .recognizer import (
    DEFAULT_GEMINI_MODEL,
    GeminiRecognizer,
    Recognizer,
    RecognizerError,
    create_recognizer,
    resolve_api_key,
)
from .roster import load_roster, roster_issues, update_roster_ocr
from .merge import BattleResult, MatchedPlayer, Reconciliation, merge_battle
from .storage import league_filename, write_battle
from .session import CapturedScreen, LeagueCaptureSession, recognize_screen

__all__ = [
    "ALL_METRICS",
    "BATTLEFIELD_LABEL",
    "DPS_METRICS",
    "LABELS",
    "SUPPORT_METRICS",
    "VIEW_LABEL",
    "VIEW_METRICS",
    "Battlefield",
    "LeagueRow",
    "PlayerBattleStats",
    "Scan",
    "View",
    "DEFAULT_GEMINI_MODEL",
    "GeminiRecognizer",
    "Recognizer",
    "RecognizerError",
    "create_recognizer",
    "resolve_api_key",
    "load_roster",
    "roster_issues",
    "update_roster_ocr",
    "BattleResult",
    "MatchedPlayer",
    "Reconciliation",
    "merge_battle",
    "league_filename",
    "write_battle",
    "CapturedScreen",
    "LeagueCaptureSession",
    "recognize_screen",
]
