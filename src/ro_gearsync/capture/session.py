"""End-to-end capture loop.

Capture runs as a **producer / consumer pipeline** with the swipe-and-shoot
work overlapped against OCR inference — the two are roughly balanced (~2 s
each), so pipelining cuts wall time by 30–40 % vs the sequential version.

  * Producer thread (this side talks to ADB)
      1. screencap → push image to queue
      2. issue swipe
      3. settle delay
      → repeat until the consumer asks to stop or ``max_pages`` is hit.

  * Consumer (main thread)
      1. pop image
      2. run primary OCR + row parse
      3. (optional) re-OCR low-confidence rows with a heavier fallback engine
      4. merge into the running dedup table
      5. invoke the ``progress`` callback so the GUI can repaint
      → stop after ``max_idle_pages`` consecutive zero-new-row pages, then
        signal the producer to bail.

The consumer keeps state, so dedup detection naturally lives on its side —
the producer just sprays pages.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Union

import cv2
import numpy as np

from ..adb import AdbClient
from ..utils.logging import logger
from ..utils.paths import user_data_dir
from ..vision import OcrEngine, MemberRow, parse_member_page
from ..vision.layout import DEFAULT_LAYOUT, GuildPageLayout
from ..vision.parser import NicknameCandidate, refine_nicknames


@dataclass
class CapturedMember:
    """A single guild member as accumulated across the whole capture run."""

    dedup_key: str
    nickname: str | None
    nickname_confidence: float | None
    gear_score: int
    gear_score_confidence: float
    candidates: list[NicknameCandidate] = field(default_factory=list)
    first_seen_page: int = 0
    first_seen_y: int = 0
    sightings: int = 1
    # Optional per-row secondary metrics (OCR-parsed from the same row).
    # ``None`` means OCR didn't find a confident value for that cell.
    weekly_contribution: int | None = None
    weekly_activity: int | None = None


@dataclass
class PageRecord:
    page_index: int
    image_path: Path
    row_count: int
    new_rows: int
    timestamp: str


@dataclass
class CaptureResult:
    session_dir: Path
    members: list[CapturedMember]
    pages: list[PageRecord]
    started_at: str
    finished_at: str
    duration_seconds: float
    halt_reason: str


@dataclass
class _PageJob:
    """Producer → consumer payload."""
    page_idx: int
    image: np.ndarray
    image_path: Path


@dataclass
class _StopMarker:
    """Producer → consumer end-of-stream marker."""
    reason: str
    error: BaseException | None = None


_QueueItem = Union[_PageJob, _StopMarker]


class CaptureSession:
    """Single capture run. Re-instantiate for each user-triggered scan.

    Pass ``fallback_ocr`` to engage the hybrid OCR strategy: any row whose
    primary OCR result is missing or below ``fallback_threshold`` gets the
    ROI re-OCR'd with the heavier engine. Empirically the fallback fires on
    ~10 rows per 150 — i.e. ~10 extra seconds on top of the primary pass.
    """

    def __init__(
        self,
        adb: AdbClient,
        ocr: OcrEngine,
        layout: GuildPageLayout = DEFAULT_LAYOUT,
        *,
        session_root: Path | None = None,
        scroll_settle_seconds: float = 0.6,
        # 650 (was 500/900): 500ms was fast enough to clear long-press
        # detection, but the resulting fling momentum scrolled past
        # several members between captures (user reported "中間很多人都
        #被略過了"). Bumping to 650ms slows the swipe enough that
        # inertia doesn't overshoot — still well below the ~1s long-
        # press threshold.
        swipe_duration_ms: int = 650,
        # 0.45 (was 0.55): shorter swipe distance = less inertia after
        # touch-up = no rows skipped between pages. We need a couple
        # more pages to cover the same list (max_pages caps at 50, so
        # plenty of headroom for a 150-member guild).
        swipe_distance_ratio: float = 0.45,
        # 55 (was 50): shorter swipe distance (0.45 vs 0.55) means we
        # need more pages to cover the same list — roughly 150/3 = 50
        # pages of unique content + 3 idle + 1 stuck = 54. 55 keeps
        # comfortable headroom. Producer finishes ~100s of captures.
        max_pages: int = 55,
        # 3 (was 2): one extra page of "saw 0 new" before we conclude
        # the list ran out. Gives a small safety buffer against rare
        # gear-score collisions that masquerade as duplicates and end
        # the scan a page or two too early.
        max_idle_pages: int = 3,
        prime_with_up_swipes: int = 2,
        fallback_ocr: OcrEngine | None = None,
        # 0.90 (was 0.80): more aggressive fallback triggers — most
        # low-confidence rows are gender-icon ingestion or stylised
        # fonts that v5-server handles much better than v5-mobile.
        # User picked 0.90 after the 2026-05-22 experiment showed 0.92
        # was too eager on already-clean rows. Tunable via config.ini's
        # [ocr] fallback_threshold key.
        fallback_threshold: float = 0.90,
        # ``None`` (default) ⇒ auto-match ``max_pages`` in __init__ so
        # the producer can race to the end without ever blocking. Pass
        # an integer explicitly to override (e.g. on memory-constrained
        # machines). Memory cost: each frame ~6MB, so 50 frames ≈ 300MB
        # peak.
        queue_size: int | None = None,
        # Anti-pattern shaping: randomise duration, distance, end-Y, and
        # settle so each swipe looks less like a metronome. Y safety is
        # tight on purpose — too much variance loses members between
        # pages. See PLAN.md §13 and project-anticheat-observation memory
        # for the why.
        humanise_swipe: bool = True,
        swipe_duration_jitter_ms: int = 150,        # ±150 ms (was ±300)
        swipe_distance_jitter_ratio: float = 0.07,  # ±0.07 of usable height
        swipe_end_y_jitter_px: int = 22,            # ±22 px
        scroll_settle_jitter_seconds: float = 0.4,  # ±0.4 s
    ) -> None:
        self.adb = adb
        self.ocr = ocr
        self.layout = layout
        self.scroll_settle_seconds = scroll_settle_seconds
        self.swipe_duration_ms = swipe_duration_ms
        self.swipe_distance_ratio = swipe_distance_ratio
        self.max_pages = max_pages
        self.max_idle_pages = max_idle_pages
        self.prime_with_up_swipes = prime_with_up_swipes
        self.fallback_ocr = fallback_ocr
        self.fallback_threshold = fallback_threshold
        self.humanise_swipe = humanise_swipe
        self.swipe_duration_jitter_ms = swipe_duration_jitter_ms
        self.swipe_distance_jitter_ratio = swipe_distance_jitter_ratio
        self.swipe_end_y_jitter_px = swipe_end_y_jitter_px
        self.scroll_settle_jitter_seconds = scroll_settle_jitter_seconds

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = session_root or user_data_dir() / "captures"
        self.session_dir = root / ts
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir = self.session_dir / "pages"
        self.pages_dir.mkdir(exist_ok=True)
        self._started_perf: float | None = None

        # Pipeline state. queue_size=None auto-matches max_pages so the
        # producer can race to the end of the list without ever blocking
        # on a full queue (the design intent of the producer/consumer
        # split — see §8.6 of PLAN.md).
        effective_queue_size = queue_size if queue_size is not None else max_pages
        self._queue: Queue[_QueueItem] = Queue(maxsize=effective_queue_size)
        self._stop_event = threading.Event()
        # Set lazily by run() — producer fires it after every successful
        # screencap so the GUI can show a "截圖: N/45" progress line.
        self._capture_progress: Callable[[int, int], None] | None = None

    # ----------------------------------------------------------------- API

    def run(
        self,
        *,
        progress: Callable[[int, int, int], None] | None = None,
        capture_progress: Callable[[int, int], None] | None = None,
    ) -> CaptureResult:
        """Run the full capture loop on a producer/consumer pipeline.

        ``progress(page_index, total_members, new_in_page)`` fires once per
        page in the consumer thread (the main thread). Heavy work should
        not happen inside the callback.

        ``capture_progress(captured_count, max_pages)`` fires from the
        PRODUCER thread once per successful screencap. Used by the GUI
        to show a separate "截圖: N/45" line and to alert the user when
        the camera phase is done (so they can use LDPlayer again).
        """
        self._capture_progress = capture_progress
        started_dt = datetime.now()
        self._started_perf = time.perf_counter()
        logger.info(
            "capture session start dir={} max_pages={} fallback={}",
            self.session_dir, self.max_pages,
            self.fallback_ocr.model_quality if self.fallback_ocr else "off",
        )

        # Safety belt: scroll to top BEFORE the producer starts — primer
        # swipes are sequential by design (no consumer state to keep busy).
        self._prime_to_top()

        # Reset pipeline state for a fresh run.
        self._stop_event.clear()
        # Drain any leftovers from an earlier run.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Empty:
                break

        producer = threading.Thread(
            target=self._producer_loop, name="capture-producer", daemon=True,
        )
        producer.start()

        try:
            members, pages, halt_reason = self._consumer_loop(progress)
        finally:
            # Always release the producer so it doesn't deadlock on a full
            # queue if the consumer crashed mid-flight.
            self._stop_event.set()
            try:
                while True:
                    self._queue.get_nowait()
            except Empty:
                pass
            producer.join(timeout=10.0)

        finished_dt = datetime.now()
        duration = (
            (time.perf_counter() - self._started_perf)
            if self._started_perf is not None
            else 0.0
        )
        result = CaptureResult(
            session_dir=self.session_dir,
            members=sorted(
                members.values(),
                key=lambda m: (m.first_seen_page, m.first_seen_y),
            ),
            pages=pages,
            started_at=started_dt.isoformat(timespec="seconds"),
            finished_at=finished_dt.isoformat(timespec="seconds"),
            duration_seconds=duration,
            halt_reason=halt_reason,
        )
        self._persist_summary(result)
        logger.info(
            "capture done unique={} pages={} halt={} in {:.1f}s",
            len(result.members), len(result.pages), halt_reason, duration,
        )
        return result

    # ------------------------------------------------------------- pipeline

    def _producer_loop(self) -> None:
        """Capture frames as fast as the queue drains.

        Fires ``self._capture_progress(captured_count, max_pages)`` after
        each successful screencap so the GUI can show "截圖: N/45".
        When all ``max_pages`` are captured the producer ALSO disconnects
        ADB right here (rather than waiting for the consumer to finish)
        — that minimises anti-cheat exposure and lets the user touch
        LDPlayer again as soon as the camera phase is done.
        """
        try:
            for page_idx in range(self.max_pages):
                if self._stop_event.is_set():
                    self._queue.put(_StopMarker("stopped_by_consumer"))
                    return
                image, image_path = self._capture_page(page_idx)
                self._queue.put(_PageJob(page_idx, image, image_path))
                if self._capture_progress is not None:
                    try:
                        self._capture_progress(page_idx + 1, self.max_pages)
                    except Exception:
                        # Never let a UI callback break the producer.
                        logger.exception("capture_progress callback failed")
                # If consumer has flipped the stop flag while we were
                # blocking on `put`, drop the next swipe.
                if self._stop_event.is_set():
                    return
                self._scroll_down(image.shape[:2])
                # Settle the list animation; consumer keeps churning meanwhile.
                if self._stop_event.wait(self._settle_delay()):
                    return
            self._queue.put(_StopMarker("max_pages"))
            # All captures done — disconnect ADB immediately so the user
            # can use LDPlayer normally while the consumer keeps OCR'ing
            # cached frames in the background. Best-effort; idempotent if
            # ScanRunner's finally also disconnects.
            try:
                self.adb.disconnect()
                logger.info("producer captured all {} pages; ADB disconnected", self.max_pages)
            except Exception as exc:  # noqa: BLE001
                logger.warning("producer's ADB disconnect failed: {}", exc)
        except BaseException as exc:  # noqa: BLE001
            logger.exception("producer thread crashed")
            try:
                self._queue.put(_StopMarker("error", error=exc), timeout=5.0)
            except Exception:
                pass

    def _consumer_loop(
        self, progress: Callable[[int, int, int], None] | None,
    ) -> tuple[dict[str, CapturedMember], list[PageRecord], str]:
        import hashlib

        members: dict[str, CapturedMember] = {}
        pages: list[PageRecord] = []
        idle = 0
        halt_reason = "max_pages"
        # Downsampled hash of the previous page's screencap. Two
        # consecutive identical thumbnails mean the swipe didn't move
        # the list — either a transient hiccup (popup, anti-cheat
        # dampening) OR we're at the bottom of the list. We can't tell
        # those apart from a single pair of frames, so we give exactly
        # ONE "free" stuck page (transient hiccup), then start counting
        # subsequent stuck pages toward idle so end-of-list detection
        # still works.
        last_thumb_hash: str | None = None
        stuck_run: int = 0  # consecutive stuck-frame count, reset on real scroll

        while True:
            item = self._queue.get()
            if isinstance(item, _StopMarker):
                if item.error is not None:
                    raise item.error
                halt_reason = item.reason
                break

            rows = parse_member_page(item.image, self.ocr, self.layout)
            # Heavy fallback re-OCR — only fires on rows that primary missed
            # or were low-confidence; cheap on average.
            if self.fallback_ocr is not None:
                refine_nicknames(
                    item.image, rows, self.fallback_ocr, self.layout,
                    minimum_confidence=self.fallback_threshold,
                )
            rows = self._filter_complete(item.image, rows)
            new_count = self._merge(rows, members, item.page_idx)

            # Scroll-stuck detection: cheap perceptual hash on a 32×18
            # thumbnail. Real scrolls move pixels visibly even after
            # downsampling, so identical thumbnail hashes mean "the
            # game didn't actually scroll between these two screencaps".
            thumb = cv2.resize(item.image, (32, 18))
            thumb_hash = hashlib.md5(thumb.tobytes()).hexdigest()
            scroll_stuck = (
                last_thumb_hash is not None and thumb_hash == last_thumb_hash
            )
            last_thumb_hash = thumb_hash

            pages.append(
                PageRecord(
                    page_index=item.page_idx,
                    image_path=item.image_path,
                    row_count=len(rows),
                    new_rows=new_count,
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                )
            )
            logger.info(
                "page #{} rows={} new={} total_unique={} queue={}{}",
                item.page_idx, len(rows), new_count, len(members),
                self._queue.qsize(),
                "  ⚠ scroll stuck (skipping idle bump)" if scroll_stuck else "",
            )
            if progress:
                progress(item.page_idx, len(members), new_count)

            if scroll_stuck:
                stuck_run += 1
            else:
                stuck_run = 0

            if new_count == 0:
                if scroll_stuck and stuck_run <= 1:
                    # First stuck page in this run — could be a
                    # transient hiccup. Give it ONE free pass. The
                    # very next stuck page falls through to idle.
                    logger.info(
                        "page #{} scroll didn't advance — giving 1 free pass",
                        item.page_idx,
                    )
                    continue
                # Either scroll moved (real "no new rows") or we've
                # been stuck for 2+ pages (we're at the bottom OR the
                # scroll is genuinely broken — either way we should stop).
                idle += 1
                if idle >= self.max_idle_pages:
                    halt_reason = (
                        "scroll_stuck_at_end"
                        if stuck_run >= self.max_idle_pages
                        else "no_new_rows"
                    )
                    self._stop_event.set()
                    # Drain anything the producer pushed after we decided to stop.
                    while True:
                        try:
                            leftover = self._queue.get_nowait()
                        except Empty:
                            break
                        if isinstance(leftover, _StopMarker):
                            break
                    break
            else:
                idle = 0
        return members, pages, halt_reason

    # ----------------------------------------------------------------- guts

    def _capture_page(self, page_idx: int) -> tuple[np.ndarray, Path]:
        png = self.adb.screencap_png()
        path = self.pages_dir / f"page_{page_idx:03d}.png"
        path.write_bytes(png)
        buf = np.frombuffer(png, dtype=np.uint8)
        image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"failed to decode screencap for page {page_idx}")
        return image, path

    def _filter_complete(
        self, image: np.ndarray, rows: list[MemberRow]
    ) -> list[MemberRow]:
        """Drop rows the consumer can't trust.

        Asymmetric edge margins:
          * TOP: 1% (≈ 11 px @ 1080p). The first row of a freshly-
            scrolled page often anchors a couple of pixels above
            ``y_top`` (e.g. page_008 of the 2026-05-19 session — row
            "品鮑教父" lands at y=322 while a symmetric 4% margin
            would cut off at y=323, missing the row by 1 px). We've
            never seen top-edge phantoms in practice, so being generous
            here doesn't introduce false positives.
          * BOTTOM: 4% (≈ 43 px @ 1080p). Partial-render artifacts at
            the bottom edge are the original reason this filter exists
            (concrete cases: page_004 y=847 "能木棚" conf=0.53 ;
            page_026 y=846 "疯狂喑磨陰亮" conf=0.63). Keep the strict
            cutoff so those still get dropped.

        Empty-nickname rows are kept on purpose — they surface in the
        post-scan review dialog with a cropped screenshot so the user
        can hand-verify what OCR couldn't read. Dedup by gear_score
        still wins; a later scroll that catches the name supersedes.
        """
        H = image.shape[0]
        y_top, y_bottom = self.layout.rows_y_pixels(H)
        top_margin = int(H * 0.01)
        bottom_margin = int(H * 0.04)
        kept: list[MemberRow] = []
        for row in rows:
            if row.row_y < y_top + top_margin:
                continue
            if row.row_y > y_bottom - bottom_margin:
                continue
            kept.append(row)
        return kept

    def _merge(
        self,
        rows: list[MemberRow],
        members: dict[str, CapturedMember],
        page_idx: int,
    ) -> int:
        added = 0
        for row in rows:
            key = _dedup_key(row)
            if key in members:
                members[key].sightings += 1
                # Keep the highest-confidence nickname seen so far.
                if (
                    row.nickname is not None
                    and row.nickname_confidence is not None
                    and (
                        members[key].nickname_confidence is None
                        or row.nickname_confidence > members[key].nickname_confidence
                    )
                ):
                    members[key].nickname = row.nickname
                    members[key].nickname_confidence = row.nickname_confidence
                for cand in row.candidates:
                    if cand not in members[key].candidates:
                        members[key].candidates.append(cand)
                # Fill in secondary metrics if the first sighting missed them.
                if members[key].weekly_contribution is None:
                    members[key].weekly_contribution = row.weekly_contribution
                if members[key].weekly_activity is None:
                    members[key].weekly_activity = row.weekly_activity
                continue
            members[key] = CapturedMember(
                dedup_key=key,
                nickname=row.nickname,
                nickname_confidence=row.nickname_confidence,
                gear_score=row.gear_score,
                gear_score_confidence=row.gear_score_confidence,
                candidates=list(row.candidates),
                first_seen_page=page_idx,
                first_seen_y=row.row_y,
                weekly_contribution=row.weekly_contribution,
                weekly_activity=row.weekly_activity,
            )
            added += 1
        return added

    def _scroll_down(self, image_shape: tuple[int, int]) -> None:
        h, w = image_shape
        cx = w // 2
        y_top, y_bottom = self.layout.rows_y_pixels(h)
        usable = max(1, y_bottom - y_top)

        # Base values
        ratio = self.swipe_distance_ratio
        duration = self.swipe_duration_ms
        from_y = y_top + int(usable * 0.85)
        to_y = max(y_top + 10, from_y - int(usable * ratio))

        if self.humanise_swipe:
            # Distance jitter — clamp ±jitter around the base ratio, but
            # never below 0.40 (would lose row overlap) or above 0.65
            # (would skip rows).
            ratio = max(
                0.40, min(
                    0.65,
                    ratio + random.uniform(
                        -self.swipe_distance_jitter_ratio,
                        self.swipe_distance_jitter_ratio,
                    ),
                ),
            )
            duration = max(
                # 250 (was 400): floor on the jittered swipe duration.
                # Lowered together with the swipe_duration_ms default
                # so the fastest jittered swipe still completes well
                # within the game's long-press threshold (~600 ms).
                250,
                duration + random.randint(
                    -self.swipe_duration_jitter_ms,
                    self.swipe_duration_jitter_ms,
                ),
            )
            distance = int(usable * ratio)
            from_y = y_top + int(usable * 0.85) + random.randint(-8, 8)
            to_y = from_y - distance + random.randint(
                -self.swipe_end_y_jitter_px,
                self.swipe_end_y_jitter_px,
            )
            # Make sure we still scroll downward AND end inside the band.
            to_y = max(y_top + 10, min(y_bottom - 10, to_y))
            # Slight x jitter so successive swipes don't form a perfectly
            # straight column. The game doesn't care about x at all for
            # scrolling, so this is purely a pattern-breaking move.
            cx = cx + random.randint(-30, 30)
        else:
            distance = int(usable * ratio)

        logger.debug(
            "swipe ({}, {}) -> ({}, {}) duration={} ratio={:.2f}",
            cx, from_y, cx, to_y, duration, ratio,
        )
        self.adb.swipe(cx, from_y, cx, to_y, duration_ms=duration)

    def _settle_delay(self) -> float:
        if not self.humanise_swipe or self.scroll_settle_jitter_seconds <= 0:
            return self.scroll_settle_seconds
        jitter = random.uniform(
            -self.scroll_settle_jitter_seconds,
            self.scroll_settle_jitter_seconds,
        )
        return max(0.2, self.scroll_settle_seconds + jitter)

    def _prime_to_top(self) -> None:
        """A few up-swipes nudge the list to the top; harmless if already there."""
        if self.prime_with_up_swipes <= 0:
            return
        # We don't have an image yet, so fall back to ADB's reported size.
        try:
            w, h = self.adb.screen_size()
        except Exception:
            return
        # The reported size is portrait-native but the game renders landscape;
        # use the larger dim as width.
        w, h = max(w, h), min(w, h)
        cx = w // 2
        y_top, y_bottom = self.layout.rows_y_pixels(h)
        usable = max(1, y_bottom - y_top)
        for _ in range(self.prime_with_up_swipes):
            self.adb.swipe(
                cx, y_top + int(usable * 0.2),
                cx, y_top + int(usable * 0.9),
                duration_ms=self.swipe_duration_ms,
            )
            time.sleep(self.scroll_settle_seconds)

    # ----------------------------------------------------------------- output

    def _persist_summary(self, result: CaptureResult) -> None:
        # Members as JSON (audit / fuzzy-match fixture).
        members_json = [
            {
                "dedup_key": m.dedup_key,
                "nickname": m.nickname,
                "nickname_confidence": m.nickname_confidence,
                "gear_score": m.gear_score,
                "gear_score_confidence": m.gear_score_confidence,
                "candidates": [asdict(c) for c in m.candidates],
                "first_seen_page": m.first_seen_page,
                "first_seen_y": m.first_seen_y,
                "sightings": m.sightings,
                "weekly_contribution": m.weekly_contribution,
                "weekly_activity": m.weekly_activity,
            }
            for m in result.members
        ]
        (self.session_dir / "members.json").write_text(
            json.dumps(members_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        pages_json = [
            {
                "page_index": p.page_index,
                "image": p.image_path.name,
                "row_count": p.row_count,
                "new_rows": p.new_rows,
                "timestamp": p.timestamp,
            }
            for p in result.pages
        ]
        summary = {
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "duration_seconds": result.duration_seconds,
            "halt_reason": result.halt_reason,
            "unique_members": len(result.members),
            "pages": pages_json,
        }
        (self.session_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# --------------------------------------------------------- module-level helpers


def _dedup_key(row: MemberRow) -> str:
    """In-session dedup key.

    Gear-score is overwhelmingly the right key:

    * Gear-score OCR has been 100% accurate across every page of the
      validation capture; the recognition score is consistently > 0.998.
    * Nicknames drift between captures whenever the row renders at a
      slightly different y (anti-aliasing, sub-pixel offsets), so including
      them over-fragments the dedup table.
    * Within a single ~150-member guild, two non-trivial gear scores
      colliding is vanishingly unlikely.

    Exception: ``gear_score == 0`` is genuinely possible (a brand-new
    member who hasn't equipped anything) and could legitimately collide
    across multiple players. Fall back to the nickname in that case so we
    don't merge several different rookies into one row.
    """
    if row.gear_score == 0:
        return f"0|{(row.nickname or '').strip()}"
    return str(row.gear_score)
