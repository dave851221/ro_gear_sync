"""Guild-member page geometry.

The values are ratios of the captured screenshot — so the same layout works
whether the user runs LDPlayer at 1280x720, 1920x1080, or any other
landscape resolution, as long as the in-game UI uses the same template
(which it does).

These ratios were measured against a real 1920x1080 capture from RO mobile
on 2026-05-18. Re-measure if the game ever ships a major UI change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class GuildPageLayout:
    """Normalised ROI definitions for the guild-member list page."""

    # Vertical band where actual member rows live (excludes header and the
    # bottom panel / broadcast strip).
    rows_top: float = 0.26
    rows_bottom: float = 0.82

    # Horizontal band for the gear-score column. Used as the anchor: every
    # row has exactly one high-confidence 4-7 digit number here.
    gear_score_left: float = 0.535
    gear_score_right: float = 0.605

    # Horizontal band for the nickname column (excluding the avatar icon).
    nickname_left: float = 0.215
    nickname_right: float = 0.365

    # Horizontal band for the "weekly / total contribution" column. The cell
    # text is rendered as "WEEK/TOTAL" (e.g. "1591/12160"); we extract the
    # numerator (this-week's contribution).
    contribution_left: float = 0.640
    contribution_right: float = 0.705

    # Horizontal band for the "weekly activity" column. Single integer.
    activity_left: float = 0.760
    activity_right: float = 0.815

    # Approximate row height when only one anchor is found (fallback).
    fallback_row_height: float = 0.105

    # Tolerance band around an anchor's centre, as a fraction of row height.
    # 0.45 was too lenient — it let the bottom-of-screen "招募喊話" UI button
    # leak into the nickname slot of the last row. Empirically the nickname
    # cy almost always sits within ±5 px of the gear-score anchor's cy, so
    # 0.20 (≈±22 px at row_h=113) is a comfortable safety belt.
    row_band_tolerance: float = 0.20

    # ----- accessors converting to absolute pixels -----

    def gear_score_x_pixels(self, width: int) -> tuple[int, int]:
        return int(width * self.gear_score_left), int(width * self.gear_score_right)

    def nickname_x_pixels(self, width: int) -> tuple[int, int]:
        return int(width * self.nickname_left), int(width * self.nickname_right)

    def contribution_x_pixels(self, width: int) -> tuple[int, int]:
        return int(width * self.contribution_left), int(width * self.contribution_right)

    def activity_x_pixels(self, width: int) -> tuple[int, int]:
        return int(width * self.activity_left), int(width * self.activity_right)

    def rows_y_pixels(self, height: int) -> tuple[int, int]:
        return int(height * self.rows_top), int(height * self.rows_bottom)

    def fallback_row_h_pixels(self, height: int) -> int:
        return int(height * self.fallback_row_height)

    def row_band_pixels(
        self, cy: float, row_height: int
    ) -> tuple[int, int]:
        half = row_height * self.row_band_tolerance
        return int(cy - half), int(cy + half)


DEFAULT_LAYOUT = GuildPageLayout()


def median(values: Iterable[float]) -> float:
    sorted_v = sorted(values)
    n = len(sorted_v)
    if n == 0:
        raise ValueError("median of empty sequence")
    mid = n // 2
    if n % 2:
        return float(sorted_v[mid])
    return (sorted_v[mid - 1] + sorted_v[mid]) / 2
