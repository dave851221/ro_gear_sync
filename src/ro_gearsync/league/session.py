"""Capture ONE league screen fast, recognise later (producer/consumer).

Mirrors the gear pipeline's split (capture races ahead, analysis lags
behind on a queue) — per the 2026-07-02 live test, waiting ~10 s for a
Gemini call between swipes made the capture phase painfully slow and kept
the user hostage. New flow:

  Capture phase (fast, ~1.5 s/page):
      screencap → stuck-check → swipe-left → settle → repeat
      Stops when the LEFT TABLE region freezes for 2 consecutive frames
      (= list hit bottom) or ``max_pages``. No recognition here at all —
      the moment this returns, the user may switch the game to the next
      screen while recognition still runs.

  Recognition phase (background thread, one Gemini call per page):
      :func:`recognize_screen` walks the captured pages, dedups rows by
      damage fingerprint, and returns the merged :class:`Scan`. Serialise
      calls across screens with a shared semaphore to stay inside the
      free-tier requests-per-minute budget.

Stuck detection crops to the left table (x 0.03–0.45, y 0.34–0.90) before
comparing, because the league screen's surroundings animate (clouds, the
勝利 sparkle) and a whole-frame hash would never settle.

Swipe distance is deliberately short (~3 rows): the user's own pinned row
at the bottom is only there when they fought that battlefield, so the
scrollable window height varies — a long swipe skips rows in one of the
two cases (observed live 2026-07-02).
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from ..adb import AdbClient
from ..matching.matcher import normalize_for_match
from ..utils.logging import logger
from .model import Battlefield, LeagueRow, Scan, View
from .recognizer import Recognizer

# Dedup fingerprint columns per view — the two highest-magnitude numbers.
# KNOWN LIMITATION (strategy): 戰略 metrics are mostly tiny (小怪/修旗/王的
# 最後一擊 are single digits, boss_damage is often 0), so the both-non-zero
# requirement rarely holds and most strategy rows fall back to the name key.
# Misread names can therefore split one player into two rows there — the
# reconciliation delta + review dialog is the safety net. boss_damage+minions
# is still the best available pair when it does fire.
_FINGERPRINT_COLS = {
    "dps": ("player_damage", "building_damage"),
    "support": ("heal", "damage_taken"),
    "strategy": ("boss_damage", "minions"),
}

# Left-table region used for the frozen-frame check (fractions of W / H).
_TABLE_X = (0.03, 0.45)
_TABLE_Y = (0.34, 0.90)
# Mean absolute pixel difference below this = "same frame".
_STUCK_DIFF_THRESHOLD = 2.0


def _dedup_key(row: LeagueRow, view: View) -> str:
    cols = _FINGERPRINT_COLS.get(view)
    if cols:
        na, nb = (row.metrics.get(c) for c in cols)
        if na and nb:
            return f"n:{na}:{nb}"
    return f"name:{normalize_for_match(row.name) or row.name}"


def _table_thumb(image: np.ndarray) -> np.ndarray:
    """Downsampled grayscale crop of the left table, for frame comparison."""
    H, W = image.shape[:2]
    x1, x2 = int(W * _TABLE_X[0]), int(W * _TABLE_X[1])
    y1, y2 = int(H * _TABLE_Y[0]), int(H * _TABLE_Y[1])
    crop = image[y1:y2, x1:x2]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (64, 32)).astype(np.int16)


@dataclass
class CapturedScreen:
    """Output of the capture phase — pages on disk, ready for recognition."""
    battlefield: Battlefield
    view: View
    session_dir: Path
    pages: list[Path] = field(default_factory=list)
    halt_reason: str = "max_pages"


class LeagueCaptureSession:
    """Fast screenshot loop for a single league screen (no recognition)."""

    def __init__(
        self,
        adb: AdbClient,
        battlefield: Battlefield,
        view: View,
        *,
        session_root: Path | None = None,
        max_pages: int = 30,
        scroll_settle_seconds: float = 0.8,
        # Slow-ish drag so fling inertia stays small — inertia is what made
        # the 0.38-height swipe overshoot in the live test.
        swipe_duration_ms: int = 750,
        # Left-list x position (fraction of width) — stay well inside the
        # left table so the right enemy list never scrolls.
        swipe_x_ratio: float = 0.22,
        # Short swipe: ~0.25 of height ≈ 3 rows. Overlap of ~4 rows per page
        # keeps dedup stitching safe whether or not the pinned self-row
        # shrinks the scrollable area.
        swipe_from_y_ratio: float = 0.72,
        swipe_to_y_ratio: float = 0.47,
    ) -> None:
        self.adb = adb
        self.battlefield = battlefield
        self.view = view
        self.max_pages = max_pages
        self.scroll_settle_seconds = scroll_settle_seconds
        self.swipe_duration_ms = swipe_duration_ms
        self.swipe_x_ratio = swipe_x_ratio
        self.swipe_from_y_ratio = swipe_from_y_ratio
        self.swipe_to_y_ratio = swipe_to_y_ratio

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        from ..utils.paths import user_data_dir
        root = session_root or (user_data_dir() / "league_captures")
        self.session_dir = root / f"{ts}_{battlefield}_{view}"
        self.pages_dir = self.session_dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)

    def capture(
        self,
        *,
        progress: Callable[[int], None] | None = None,
    ) -> CapturedScreen:
        """Shoot pages until the left table freezes. ``progress(n_pages)``
        fires after each saved page. Fast — never calls the recognizer."""
        result = CapturedScreen(
            battlefield=self.battlefield,
            view=self.view,
            session_dir=self.session_dir,
        )
        last_thumb: np.ndarray | None = None
        stuck = 0
        logger.info("league capture start {}/{}", self.battlefield, self.view)

        for page_idx in range(self.max_pages):
            image, path = self._capture_page(page_idx)
            thumb = _table_thumb(image)
            if last_thumb is not None:
                diff = float(np.abs(thumb - last_thumb).mean())
                if diff < _STUCK_DIFF_THRESHOLD:
                    stuck += 1
                    logger.info(
                        "league page #{} table frozen (diff={:.2f}, run={})",
                        page_idx, diff, stuck,
                    )
                    # Identical frame — drop it (no point paying Gemini for
                    # a duplicate) and stop once we've seen two in a row.
                    path.unlink(missing_ok=True)
                    if stuck >= 2:
                        # One LAST shot with the drag HELD: the pinned
                        # self-row (when present) permanently covers the
                        # bottom slot, so the final member is only visible
                        # while the list is rubber-band over-scrolled.
                        # Drag up, DON'T release, screencap, then release.
                        held = self._capture_held_overscroll(
                            page_idx, image.shape[:2],
                        )
                        if held is not None:
                            result.pages.append(held)
                            if progress:
                                progress(len(result.pages))
                        result.halt_reason = "list_bottom"
                        break
                    self._scroll_left(image.shape[:2])
                    time.sleep(self.scroll_settle_seconds)
                    continue
            stuck = 0
            last_thumb = thumb
            result.pages.append(path)
            if progress:
                progress(len(result.pages))
            self._scroll_left(image.shape[:2])
            time.sleep(self.scroll_settle_seconds)

        logger.info(
            "league capture done {}/{}: {} pages ({})",
            self.battlefield, self.view, len(result.pages), result.halt_reason,
        )
        return result

    # ----------------------------------------------------------------- guts

    def _capture_page(self, page_idx: int) -> tuple[np.ndarray, Path]:
        png = self.adb.screencap_png()
        path = self.pages_dir / f"page_{page_idx:03d}.png"
        path.write_bytes(png)
        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"failed to decode league screencap page {page_idx}")
        return image, path

    def _scroll_left(self, image_shape: tuple[int, int]) -> None:
        h, w = image_shape
        x = int(w * self.swipe_x_ratio)
        from_y = int(h * self.swipe_from_y_ratio)
        to_y = int(h * self.swipe_to_y_ratio)
        self.adb.swipe(x, from_y, x, to_y, duration_ms=self.swipe_duration_ms)

    # Held-shot drag parameters. LDPlayer's Android lacks ``input
    # motionevent`` (verified 2026-07-07), so we can't decompose the
    # gesture into DOWN/MOVE/…/UP. Equivalent trick: issue one LONG
    # ``input swipe`` (the pointer stays pressed and moves linearly for
    # the whole duration) and screencap from a parallel thread at ~70%
    # of the duration — the finger is still down, the list is held in
    # rubber-band over-scroll.
    _HELD_SWIPE_DURATION_MS = 4000
    _HELD_SHOT_AT_FRACTION = 0.70

    def _capture_held_overscroll(self, page_idx: int, image_shape) -> "Path | None":
        """Screenshot taken while a drag is HELD past the list bottom.

        While held, rows shift up but the pinned self-row (a UI overlay)
        doesn't — so the member it normally covers becomes visible for
        exactly this one frame. Returns the saved page path, or None when
        anything goes wrong (capture then simply ends without the extra
        page; reconciliation will flag the missing member).
        """
        h, w = image_shape
        x = int(w * self.swipe_x_ratio)
        from_y = int(h * self.swipe_from_y_ratio)
        # Drag far — over-scroll displacement saturates, more is harmless.
        to_y = int(h * 0.30)
        swipe_err: list[BaseException] = []

        def _long_swipe() -> None:
            try:
                self.adb.swipe(
                    x, from_y, x, to_y, duration_ms=self._HELD_SWIPE_DURATION_MS,
                )
            except BaseException as exc:  # noqa: BLE001
                swipe_err.append(exc)

        swiper = threading.Thread(
            target=_long_swipe, name="league-held-swipe", daemon=True,
        )
        try:
            swiper.start()
            time.sleep(
                self._HELD_SWIPE_DURATION_MS * self._HELD_SHOT_AT_FRACTION / 1000.0
            )
            _, path = self._capture_page(page_idx)
            logger.info("league held over-scroll page captured: {}", path.name)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("held over-scroll capture failed（略過加拍）: {}", exc)
            return None
        finally:
            # Let the swipe finish (= finger release) before returning, so
            # the caller never issues ADB input on top of a live gesture.
            swiper.join(timeout=self._HELD_SWIPE_DURATION_MS / 1000.0 + 10)
            if swipe_err:
                logger.warning("held over-scroll swipe errored: {}", swipe_err[0])


# ------------------------------------------------------------- recognition


def recognize_screen(
    recognizer: Recognizer,
    captured: CapturedScreen,
    *,
    progress: Callable[[int, int, int], None] | None = None,
    retry_notice: Callable[[str], None] | None = None,
    gate: threading.Semaphore | None = None,
) -> Scan:
    """Recognise every captured page and merge rows into one :class:`Scan`.

    Runs happily on a background thread. ``progress(done, total, unique)``
    fires after each page; ``retry_notice(message)`` fires when the backend
    is stalling on transient errors (so the UI can say why it's slow).
    Pass one shared ``gate`` (Semaphore(1)) across concurrent screens to
    serialise Gemini calls and respect the free-tier RPM budget.
    """
    merged: dict[str, LeagueRow] = {}
    sightings: dict[str, int] = {}
    # Every page shows the same 參戰人數 in the corner, so a per-page
    # misread is best voted away — majority across pages wins (a single
    # "last page overrides" rule let one bad read poison the tally).
    pc_votes: Counter = Counter()
    total = len(captured.pages)

    for i, path in enumerate(captured.pages):
        image = cv2.imread(str(path))
        if image is None:
            logger.warning("league recognise: unreadable page {}", path)
            continue
        with gate if gate is not None else nullcontext():
            scan = recognizer.recognize(
                image, captured.battlefield, captured.view,
                on_retry=retry_notice,
            )
        if scan.participant_count is not None:
            pc_votes[scan.participant_count] += 1
        for row in scan.rows:
            key = _dedup_key(row, captured.view)
            existing = merged.get(key)
            if existing is None:
                merged[key] = row
                sightings[key] = 1
                continue
            sightings[key] += 1
            for k, v in row.metrics.items():
                if existing.metrics.get(k) is None and v is not None:
                    existing.metrics[k] = v
            # Same fingerprint seen again — a longer name means an earlier
            # sighting was truncated (e.g. 「暮而歸ㄋ布o」 read as 「暮而歸」
            # on the page where the row sat at the edge). Upgrade.
            if len(row.name) > len(existing.name):
                existing.name = row.name
        if progress:
            progress(i + 1, total, len(merged))

    rows = _final_collapse(merged, sightings)
    participant_count = pc_votes.most_common(1)[0][0] if pc_votes else None
    return Scan(
        battlefield=captured.battlefield,
        view=captured.view,
        participant_count=participant_count,
        rows=rows,
    )


def _final_collapse(
    merged: dict[str, LeagueRow], sightings: dict[str, int],
) -> list[LeagueRow]:
    """Collapse residual duplicates the per-page dedup can't catch.

    Two real-world leak classes (live run 2026-07-02):

      * **Pinned self-row bleed-through**: the player's pinned bottom row
        overlays another row, and the occluded numbers bleed into one
        metric → same name, several fingerprints. Names are unique within
        one battlefield ranking, so same-normalised-name rows ARE the same
        player. Keep the sighting seen on the most pages — the natural
        (un-occluded) position appears on 2+ overlapping pages with
        consistent metrics, while each bleed variant is seen once.
      * **Edge-cut misreads**: a row cut at the page border gets its name
        misread but metrics intact → identical metric tuples under two
        names. If the full tuple matches and carries a big (≥1000) value,
        merge and keep the longer name.
    """
    # Pass 1 — identical full metric tuple (with a big value) = same player.
    by_tuple: dict[tuple, str] = {}
    for key in list(merged):
        row = merged[key]
        tup = tuple(row.metrics.get(k) or 0 for k in sorted(row.metrics))
        if not any(v >= 1000 for v in tup):
            continue
        winner_key = by_tuple.get(tup)
        if winner_key is None:
            by_tuple[tup] = key
            continue
        winner = merged[winner_key]
        if len(row.name) > len(winner.name):
            winner.name = row.name
        sightings[winner_key] += sightings.pop(key, 0)
        del merged[key]
        logger.info("league collapse (metrics): merged duplicate of {!r}", winner.name)

    # Pass 2 — same normalised name = probably the same player, BUT ONLY
    # when the metrics look compatible. The guild has near-twin names
    # (岡本002 vs 岡本003) that OCR sometimes reads as the SAME string —
    # those are two real players with unrelated numbers and must NOT be
    # merged (at most one will exact-match its roster row; the other
    # goes to manual assignment in the review dialog).
    # The case we DO want to merge is pinned-row bleed-through: one metric
    # got polluted while the rest stayed identical.
    by_name: dict[str, str] = {}
    for key in list(merged):
        row = merged[key]
        name_norm = normalize_for_match(row.name) or row.name
        winner_key = by_name.get(name_norm)
        if winner_key is None:
            by_name[name_norm] = key
            continue
        winner = merged[winner_key]
        if not _metrics_compatible(winner, row):
            # Same name, clearly different numbers → treat as two players
            # (likely a misread twin name). Keep both; reconciliation +
            # roster matching will sort them out.
            logger.info(
                "league collapse (name): kept BOTH rows named {!r} — "
                "metrics differ, likely twin names misread", row.name,
            )
            continue
        # Keep whichever variant was seen on more pages (consistency = truth).
        if sightings.get(key, 0) > sightings.get(winner_key, 0):
            merged[winner_key] = merged[key]
        sightings[winner_key] = sightings.get(winner_key, 0) + sightings.pop(key, 0)
        del merged[key]
        logger.info(
            "league collapse (name): merged duplicate of {!r}",
            merged[winner_key].name,
        )

    return list(merged.values())


def _metrics_compatible(a: LeagueRow, b: LeagueRow) -> bool:
    """Do two same-named rows plausibly describe the same player?

    True when (a) they share an identical big (≥1000) value — damage-scale
    numbers colliding by chance is negligible — or (b) at least 3 of the 4
    metrics are identical (the bleed-through case: one polluted cell, the
    rest intact). All-different rows are presumed to be two real players
    whose similar names OCR collapsed.
    """
    keys = set(a.metrics) | set(b.metrics)
    equal = 0
    big_match = False
    for k in keys:
        va, vb = a.metrics.get(k) or 0, b.metrics.get(k) or 0
        if va == vb:
            equal += 1
            if va >= 1000:
                big_match = True
    return big_match or equal >= 3
