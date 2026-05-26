"""Turn a raw guild-member OCR pass into structured row records.

The algorithm:

  1.  Run OCR on the full screenshot.
  2.  Use the gear-score column as the row **anchor**. Every visible member
      row has exactly one large, high-confidence integer here, so it's the
      most reliable signal in the entire page.
  3.  For each anchor, define a horizontal band of +/- ``row_band_tolerance``
      around its vertical centre and look in the nickname column for the
      nearest text.
  4.  Optionally re-OCR each nickname ROI from a 2x-upscaled crop. This often
      lifts borderline cells (think small glyphs, traditional/simplified
      glyph variants) from ~0.8 confidence into the 0.95+ range.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import cv2
import numpy as np

from .layout import DEFAULT_LAYOUT, GuildPageLayout, median
from .ocr import OcrBox, OcrEngine, OcrResult

_DIGIT_RE = re.compile(r"[^\d]")

# Characters the v5 detector occasionally tags onto an otherwise-clean
# nickname (icon edges, watermark glyphs). They are not real player-name
# endings, so strip them. Per 2026-05-22 diagnostic on page_008, the v5 detector commonly absorbs the gender icon as one of: ? ？ ! ！ ® © ™ § @ etc.
_TRAILING_NOISE_RE = re.compile(r"[?？!！*+#$%^&~@®©™§¶ ​-‍﻿]+$")
# A handful of UI strings that show up at the bottom of the guild panel and
# can leak into the nickname column when a row sits near the lower edge.
# Treat exact matches as "no nickname" rather than as a player name.
_UI_BLACKLIST: frozenset[str] = frozenset({
    "招募喊話", "玩法相關", "玩法相閣", "更多功能",
    "公會", "公會成員", "公會資訊", "公會福利",
    "公會活動", "公會管理",
})


def _clean_nickname(text: str | None) -> str | None:
    """Drop trailing detector noise and reject UI strings.

    Returns ``None`` if the cleaned text is empty or matches the UI panel
    blacklist (so the row gets flagged as missing-nickname instead).
    """
    if not text:
        return None
    cleaned = _TRAILING_NOISE_RE.sub("", text.strip())
    if not cleaned:
        return None
    if cleaned in _UI_BLACKLIST:
        return None
    return cleaned


@dataclass
class NicknameCandidate:
    text: str
    confidence: float
    source: str  # "primary" | "roi-refined"


@dataclass
class MemberRow:
    row_index: int                    # 0-based, top to bottom on this page
    row_y: int                        # centre y of the anchor in pixels
    gear_score: int                   # parsed integer
    gear_score_text: str              # original OCR text (kept for audit)
    gear_score_confidence: float
    gear_score_box: tuple[int, int, int, int]  # x1, y1, x2, y2

    nickname: str | None = None
    nickname_confidence: float | None = None
    nickname_box: tuple[int, int, int, int] | None = None
    nickname_source: str = "primary"  # "primary" or "roi-refined"

    # Every nickname OCR attempt that produced text, including the one
    # currently surfaced via the ``nickname`` field. The matching layer
    # consults each candidate against the alias dictionary, because OCR
    # confidence is not a reliable proxy for *correctness* on stylised
    # game fonts.
    candidates: list[NicknameCandidate] = field(default_factory=list)

    # Optional secondary metrics from the same row. Both come straight from
    # the in-game header columns.
    weekly_contribution: int | None = None
    weekly_contribution_text: str = ""        # original "WEEK/TOTAL" text
    weekly_activity: int | None = None
    weekly_activity_text: str = ""


def parse_member_page(
    image: np.ndarray,
    ocr: OcrEngine | OcrResult,
    layout: GuildPageLayout = DEFAULT_LAYOUT,
) -> list[MemberRow]:
    """Identify member rows from a single guild-list screenshot.

    Pass either an ``OcrEngine`` (which will run a fresh full-image pass) or
    a pre-computed ``OcrResult`` (handy in tests).
    """
    if isinstance(ocr, OcrEngine):
        result = ocr.run(image)
    else:
        result = ocr

    H, W = image.shape[:2]
    gs_x = layout.gear_score_x_pixels(W)
    rows_y = layout.rows_y_pixels(H)

    anchors = _extract_gear_score_anchors(result.boxes, gs_x, rows_y)
    if not anchors:
        return []

    anchors.sort(key=lambda b: b.cy)
    row_height = _estimate_row_height(anchors, layout, H)

    nick_x = layout.nickname_x_pixels(W)
    contrib_x = layout.contribution_x_pixels(W)
    activity_x = layout.activity_x_pixels(W)
    rows: list[MemberRow] = []
    for i, anchor in enumerate(anchors):
        digits = _DIGIT_RE.sub("", anchor.text)
        try:
            gs_value = int(digits)
        except ValueError:
            continue
        x1, y1, x2, y2 = anchor.bbox
        row = MemberRow(
            row_index=i,
            row_y=int(anchor.cy),
            gear_score=gs_value,
            gear_score_text=anchor.text,
            gear_score_confidence=anchor.score,
            gear_score_box=(x1, y1, x2, y2),
        )
        y_low, y_high = layout.row_band_pixels(anchor.cy, row_height)
        # Belt-and-braces: the nickname must additionally lie inside the
        # rows-region of the page. A wide row_band_tolerance combined with
        # an anchor near the band edge used to let the bottom-of-screen
        # "招募喊話" UI button leak in. Clamp.
        y_low = max(y_low, rows_y[0])
        y_high = min(y_high, rows_y[1])
        candidates = _find_in_band(result.boxes, x_range=nick_x, y_range=(y_low, y_high))
        if candidates:
            best = max(candidates, key=lambda c: c.score)
            bx1, by1, bx2, by2 = best.bbox
            cleaned = _clean_nickname(best.text)
            if cleaned:
                row.nickname = cleaned
                row.nickname_confidence = best.score
                row.nickname_box = (bx1, by1, bx2, by2)
                row.candidates.append(
                    NicknameCandidate(cleaned, best.score, "primary")
                )

        # --- weekly contribution (text format "WEEK/TOTAL") --------------
        contrib_candidates = _find_in_band(
            result.boxes, x_range=contrib_x, y_range=(y_low, y_high),
        )
        if contrib_candidates:
            best_c = max(contrib_candidates, key=lambda c: c.score)
            row.weekly_contribution_text = best_c.text
            row.weekly_contribution = _parse_weekly_contribution(best_c.text)

        # --- weekly activity (single integer) ----------------------------
        activity_candidates = _find_in_band(
            result.boxes, x_range=activity_x, y_range=(y_low, y_high),
        )
        if activity_candidates:
            best_a = max(activity_candidates, key=lambda c: c.score)
            row.weekly_activity_text = best_a.text
            row.weekly_activity = _parse_int_or_none(best_a.text)
        rows.append(row)
    return rows


def _parse_weekly_contribution(text: str) -> int | None:
    """Extract the numerator from a 'WEEK/TOTAL' contribution cell.

    The in-game cell renders ``"1591/12160"`` (this-week / lifetime). We
    only care about the numerator. OCR occasionally misreads the slash as
    a 1 or T; treat any non-digit as the delimiter and take the first
    chunk.
    """
    if not text:
        return None
    # Split on the first non-digit run; first chunk is "this week".
    parts = re.split(r"\D+", text.strip())
    parts = [p for p in parts if p]
    if not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def _parse_int_or_none(text: str) -> int | None:
    if not text:
        return None
    digits = _DIGIT_RE.sub("", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def refine_nicknames(
    image: np.ndarray,
    rows: list[MemberRow],
    ocr: OcrEngine,
    layout: GuildPageLayout = DEFAULT_LAYOUT,
    *,
    upscale: float = 2.0,
    minimum_confidence: float = 0.95,
) -> list[MemberRow]:
    """Re-OCR each nickname ROI from an upscaled crop.

    Modifies ``rows`` in place (and also returns them for chaining). Cells
    that already exceed ``minimum_confidence`` are left alone unless they
    have no primary detection at all.
    """
    H, W = image.shape[:2]
    nick_x = layout.nickname_x_pixels(W)
    row_height = max(1, layout.fallback_row_h_pixels(H))
    if len(rows) >= 2:
        diffs = [rows[i + 1].row_y - rows[i].row_y for i in range(len(rows) - 1)]
        row_height = max(row_height, int(median(diffs)))

    for row in rows:
        if (
            row.nickname is not None
            and row.nickname_confidence is not None
            and row.nickname_confidence >= minimum_confidence
        ):
            continue
        roi = _crop_nickname_roi(image, row, nick_x, row_height, layout)
        if roi.size == 0:
            continue
        if upscale and upscale != 1.0:
            roi = cv2.resize(
                roi, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC
            )
        sub_result = ocr.run(roi)
        if not sub_result.boxes:
            continue

        # Avoid concatenating multiple detections on the ROI: the cell holds
        # a single nickname, and previous experiments showed that joining
        # picks up icon edges / trailing glyphs as extra characters
        # ("咿比鸭鴨、" -> "咿比甲鸭鸭、"). Take only the single largest box
        # (by polygon width) so we keep the main name and drop side noise.
        best_by_width = max(
            sub_result.boxes,
            key=lambda b: (b.bbox[2] - b.bbox[0]) * b.score,
        )
        cleaned = _clean_nickname(best_by_width.text)
        if not cleaned:
            continue

        row.candidates.append(
            NicknameCandidate(cleaned, best_by_width.score, "roi-refined")
        )

        # Only surface the refined answer if (a) primary was missing entirely
        # or (b) the refined confidence is meaningfully higher. Tie-breaking
        # at equal confidence sticks with primary.
        primary_conf = row.nickname_confidence or 0.0
        if row.nickname is None or best_by_width.score > primary_conf + 0.02:
            row.nickname = cleaned
            row.nickname_confidence = best_by_width.score
            row.nickname_source = "roi-refined"
    return rows


# ----------------------------------------------------------------- internals


def _extract_gear_score_anchors(
    boxes: Sequence[OcrBox],
    x_range: tuple[int, int],
    y_range: tuple[int, int],
    min_digits: int = 1,
) -> list[OcrBox]:
    out: list[OcrBox] = []
    for b in boxes:
        if not (x_range[0] <= b.cx <= x_range[1]):
            continue
        if not (y_range[0] <= b.cy <= y_range[1]):
            continue
        digits = _DIGIT_RE.sub("", b.text)
        if len(digits) < min_digits:
            continue
        out.append(b)
    return out


def _find_in_band(
    boxes: Iterable[OcrBox],
    *,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
) -> list[OcrBox]:
    found: list[OcrBox] = []
    for b in boxes:
        if not (x_range[0] <= b.cx <= x_range[1]):
            continue
        if not (y_range[0] <= b.cy <= y_range[1]):
            continue
        found.append(b)
    return found


def _estimate_row_height(
    anchors: Sequence[OcrBox],
    layout: GuildPageLayout,
    image_height: int,
) -> int:
    if len(anchors) >= 2:
        diffs = [
            anchors[i + 1].cy - anchors[i].cy for i in range(len(anchors) - 1)
        ]
        return max(1, int(median(diffs)))
    return max(1, layout.fallback_row_h_pixels(image_height))


def _crop_nickname_roi(
    image: np.ndarray,
    row: MemberRow,
    nick_x: tuple[int, int],
    row_height: int,
    layout: GuildPageLayout,
) -> np.ndarray:
    """Crop the *full* nickname column band for this row.

    We deliberately ignore the primary OCR bbox here: the detection step
    occasionally drops decorative glyphs at either end of the name (saw
    "-南湘楚-" come back as "南湘楚" because the detector skipped the
    en-dashes). Cropping the full column gives the detector a second pass
    with the same glyphs included, which sometimes recovers them.
    """
    H, W = image.shape[:2]
    x1, x2 = nick_x
    if row.nickname_box is not None:
        # Use the detected box's vertical extent — it tracks the actual text
        # baseline better than the row band ratio.
        _, by1, _, by2 = row.nickname_box
        pad_y = max(6, int((by2 - by1) * 0.30))
        y1 = max(0, by1 - pad_y)
        y2 = min(H, by2 + pad_y)
    else:
        y_low, y_high = layout.row_band_pixels(row.row_y, row_height)
        y1 = max(0, y_low)
        y2 = min(H, y_high)
    return image[y1:y2, max(0, x1):min(W, x2)]
