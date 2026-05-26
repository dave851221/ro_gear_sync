"""Worker-thread wrapper around :class:`CaptureSession`.

Two-phase matching during live scan, per the rewritten UX:

  Phase A (during scan)
    For each newly-deduped capture row, try ONLY exact matches against
    ``correct_nickname`` and ``latest_ocr_nickname``. If found, emit a
    "member_resolved" event with the gear-score delta against the
    player's last recorded day. If not found, hold the capture in a
    pending pool and emit "member_pending" so the UI can show a
    placeholder row (no name, no delta yet).

  Phase B (after scan finishes)
    Run fuzzy matching on the pending pool. Each fuzzy match emits
    "member_resolved" with ``decision="fuzzy_review"`` — the UI repaints
    the placeholder row red so the user notices the auto-guess. Captures
    that don't match anything at all emit "member_resolved" with
    ``decision="unmatched"``.

  Finally we emit a single "summary" event carrying:
    * the CaptureResult
    * the per-capture list of LiveMember (all resolved + pending state)
    * a list of MissedReviewItem (workbook rows the scan didn't see)
    * a list of UnmatchedReviewItem (captures with no Excel home)

Event types pushed onto :attr:`ScanRunner.events`:

  * ``("status", str)``         — human-readable progress text
  * ``("progress", page, total, new)``
  * ``("member_resolved", LiveMember)``  — name/diff finalised (exact or fuzzy)
  * ``("member_pending", LiveMember)``   — placeholder row, no match yet
  * ``("summary", SummaryPayload)``      — scan + all post-scan info
  * ``("error", Exception)``             — fatal
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from ..adb import AdbClient
from ..capture import CaptureSession
from ..capture.session import CapturedMember, CaptureResult
from ..matching import Matcher
from ..storage import PlayerRecord
from ..utils.logging import logger
from ..vision import OcrEngine
from .review_dialog import MissedReviewItem, UnmatchedReviewItem


@dataclass
class LiveMember:
    """One row in the GUI's live-scan table.

    During the scan a member may exist in one of three states:

      * ``decision="exact"``        — exact match, name + diff shown
      * ``decision="pending"``      — no exact match yet, placeholder
      * ``decision="fuzzy_review"`` — resolved post-scan via fuzzy match,
                                       UI repaints the row red
      * ``decision="unmatched"``    — no match at all, info only
    """

    ocr_nickname: str              # what OCR saw — used as fallback caption
    gear_score: int
    confidence: float | None
    matched_to: str | None          # correct_nickname when known, else None
    previous_gear: int | None
    delta: int | None
    decision: str
    record_index: int | None
    page_index: int                # which capture page yielded this row
    dedup_key: str                 # used by the UI to update in place
    # Device-coord Y of this row inside its page. Lets the review dialog
    # crop the original screenshot when the user wants a visual on
    # unmatched / empty-nickname captures.
    row_y: int = 0
    # Column-A ID of the matched workbook row. ``None`` while pending or
    # for truly-unmatched captures — the live table renders "—" then.
    player_id: int | None = None
    # Fuzzy match score (0-100). Only populated when
    # ``decision == "fuzzy_review"``. Used by the review dialog to
    # show the user how confident the fuzzy guess was.
    score: float | None = None


@dataclass
class SummaryPayload:
    """All the data the GUI needs to render the post-scan review dialog."""

    capture_day: str                       # "YYYY-MM-DD"
    result: CaptureResult | None           # None for rescan-from-session
    members: list[LiveMember] = field(default_factory=list)
    missed: list[MissedReviewItem] = field(default_factory=list)
    unmatched: list[UnmatchedReviewItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared matching logic — used by both the live scan and rescan-from-session.
# ---------------------------------------------------------------------------


def _resolve_exact(
    cm: CapturedMember,
    matcher: Matcher,
    records: Sequence[PlayerRecord],
    capture_day: str,
) -> LiveMember | None:
    """Try exact match (correct → ocr). Returns a resolved LiveMember or None."""
    nickname = (cm.nickname or "").strip()
    if not nickname:
        return None
    idx = matcher.exact_match_correct(nickname)
    if idx is None:
        idx = matcher.exact_match_ocr(nickname)
    if idx is None:
        return None
    matcher.mark_claimed(idx)
    rec = records[idx]
    prev = rec.previous_gear_before(capture_day)
    delta = cm.gear_score - prev if prev is not None else None
    return LiveMember(
        ocr_nickname=nickname,
        gear_score=cm.gear_score,
        confidence=cm.nickname_confidence,
        matched_to=rec.correct_nickname or rec.latest_ocr_nickname or nickname,
        previous_gear=prev,
        delta=delta,
        decision="exact",
        record_index=idx,
        page_index=cm.first_seen_page,
        dedup_key=cm.dedup_key,
        row_y=cm.first_seen_y,
        player_id=rec.player_id,
    )


def _resolve_fuzzy(
    cm: CapturedMember,
    matcher: Matcher,
    records: Sequence[PlayerRecord],
    capture_day: str,
) -> LiveMember:
    """Phase-3 fuzzy match against ``correct_nickname``, or mark unmatched.

    Per the 2026-05-22 spec we only fuzzy-match against the user-curated
    ``correct_nickname`` (was: also against ``latest_ocr_nickname``).
    Phase-3 hits get ``decision="fuzzy_review"`` (red live row, AND
    surfaced in the review dialog's ❶ section with an opt-in checkbox).
    """
    nickname = (cm.nickname or "").strip()
    cand = None
    if nickname:
        cand = matcher.fuzzy_match_correct(nickname)
    if cand is None:
        return LiveMember(
            ocr_nickname=nickname or "(空)",
            gear_score=cm.gear_score,
            confidence=cm.nickname_confidence,
            matched_to=None,
            previous_gear=None,
            delta=None,
            decision="unmatched",
            record_index=None,
            page_index=cm.first_seen_page,
            dedup_key=cm.dedup_key,
            row_y=cm.first_seen_y,
        )
    matcher.mark_claimed(cand.record_index)
    rec = records[cand.record_index]
    prev = rec.previous_gear_before(capture_day)
    delta = cm.gear_score - prev if prev is not None else None
    return LiveMember(
        ocr_nickname=nickname,
        gear_score=cm.gear_score,
        confidence=cm.nickname_confidence,
        matched_to=rec.correct_nickname or rec.latest_ocr_nickname or nickname,
        previous_gear=prev,
        delta=delta,
        decision="fuzzy_review",
        record_index=cand.record_index,
        page_index=cm.first_seen_page,
        dedup_key=cm.dedup_key,
        row_y=cm.first_seen_y,
        player_id=rec.player_id,
        score=cand.score,
    )


def _build_missed(
    matcher: Matcher,
    records: Sequence[PlayerRecord],
    *,
    exact_matched: set[int] | None = None,
    fuzzy_hits: dict[int, "LiveMember"] | None = None,
    pages_dir: Path | None = None,
) -> list[MissedReviewItem]:
    """Workbook rows that need user attention in the review dialog.

    Combines two kinds of items per the 2026-05-22 spec:

      * Pure missed — workbook had this row, OCR didn't see anything
        for it. Manual fill only.
      * Phase-3 fuzzy candidate — workbook had this row, OCR found a
        similar string. Carries ``candidate_*`` fields so the dialog
        can render an opt-in "套用 (gear)" checkbox alongside the
        manual fill.

    Records that exact-matched (phase A) are excluded — they're already
    applied to the workbook by the time we get here.
    """
    if exact_matched is None:
        # Fallback for callers that don't track phase-A separately —
        # behaviour matches the legacy contract (just look at claimed).
        exact_matched = set(matcher._claimed)  # noqa: SLF001
    fuzzy_hits = fuzzy_hits or {}
    out: list[MissedReviewItem] = []
    for i, rec in enumerate(records):
        if i in exact_matched:
            continue
        if not rec.gear_scores and not rec.correct_nickname and i not in fuzzy_hits:
            # Empty rows aren't worth surfacing UNLESS a fuzzy candidate
            # latched onto them (unlikely but cheap to keep safe).
            continue
        last_day = rec.last_recorded_day()
        last_val = rec.gear_scores.get(last_day) if last_day else None
        item = MissedReviewItem(
            record_index=i,
            name=rec.correct_nickname or rec.latest_ocr_nickname or "(未填)",
            last_day=last_day,
            last_value=last_val,
        )
        lm = fuzzy_hits.get(i)
        if lm is not None:
            item.candidate_gear = lm.gear_score
            item.candidate_ocr_name = lm.ocr_nickname
            item.candidate_score = lm.score
            item.candidate_confidence = lm.confidence
            item.candidate_page = lm.page_index
            item.candidate_row_y = lm.row_y
            # Resolve the page screenshot path so the review dialog can
            # crop a row-strip thumbnail for this fuzzy proposal.
            if pages_dir is not None and lm.page_index is not None:
                item.candidate_image_path = pages_dir / f"page_{lm.page_index:03d}.png"
        out.append(item)
    return out


def resolve_captures(
    captured_members: Sequence[CapturedMember],
    records: Sequence[PlayerRecord],
    capture_day: str,
    pages_dir: "Path | None" = None,
) -> tuple[list[LiveMember], list[MissedReviewItem], list[UnmatchedReviewItem]]:
    """Synchronous two-phase matcher — used by the rescan flow.

    Phase A exact-matches every capture; phase B fuzzy-matches whatever
    didn't take. The returned ``members`` list mirrors the live event
    stream so the UI can render the same table for live and rescan.

    ``pages_dir`` (when provided) lets the review dialog pull thumbnail
    crops from ``page_NNN.png`` files for unmatched captures. The
    rescan-from-session and re-OCR flows pass this through; tests can
    leave it as None.
    """
    matcher = Matcher(records)
    members: list[LiveMember] = []
    pending: list[CapturedMember] = []

    # Phase A: exact only — these write through immediately when the
    # caller eventually invokes merge_capture.
    for cm in captured_members:
        lm = _resolve_exact(cm, matcher, records, capture_day)
        if lm is not None:
            members.append(lm)
        else:
            pending.append(cm)
    # Snapshot which records were exact-matched so _build_missed can
    # exclude them. Phase B claims (fuzzy candidates) are NOT exact,
    # so we want them surfaced in the missed list as candidates.
    exact_matched: set[int] = {
        lm.record_index for lm in members if lm.record_index is not None
    }

    # Phase B: phase-3 fuzzy (correct_nickname only).
    unmatched: list[UnmatchedReviewItem] = []
    fuzzy_hits: dict[int, LiveMember] = {}
    for cm in pending:
        lm = _resolve_fuzzy(cm, matcher, records, capture_day)
        members.append(lm)
        if lm.decision == "fuzzy_review" and lm.record_index is not None:
            fuzzy_hits[lm.record_index] = lm
        elif lm.decision == "unmatched":
            image_path = None
            if pages_dir is not None and lm.page_index is not None:
                image_path = pages_dir / f"page_{lm.page_index:03d}.png"
            unmatched.append(UnmatchedReviewItem(
                ocr_nickname=lm.ocr_nickname,
                gear_score=lm.gear_score,
                confidence=lm.confidence,
                page_index=lm.page_index,
                image_path=image_path,
                row_y=lm.row_y,
            ))

    missed = _build_missed(
        matcher, records,
        exact_matched=exact_matched,
        fuzzy_hits=fuzzy_hits,
        pages_dir=pages_dir,
    )
    return members, missed, unmatched


# ---------------------------------------------------------------------------
# ScanRunner — live ADB+OCR scan, emits events progressively.
# ---------------------------------------------------------------------------


class ScanRunner:
    """Owns the worker thread + event queue for a single live scan run."""

    def __init__(
        self,
        *,
        adb: AdbClient,
        ocr: OcrEngine,
        fallback_ocr: Optional[OcrEngine],
        records: Sequence[PlayerRecord],
        # 50 (was 45): 45 occasionally capped in practice. 50 gives
        # ~5 pages of headroom over the 44-page worst case. Producer
        # races through all 50 captures in ~90s and disconnects ADB,
        # consumer keeps OCRing in the background.
        max_pages: int = 50,
        capture_day: str | None = None,
    ) -> None:
        self.adb = adb
        self.ocr = ocr
        self.fallback_ocr = fallback_ocr
        self.records = list(records)
        self.max_pages = max_pages
        self.capture_day = capture_day or date.today().isoformat()
        self.events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancelled = False
        self._result: CaptureResult | None = None
        # Filled in once CaptureSession is created so the GUI can find
        # the on-disk session folder for the "cancel and delete" flow.
        self._session_dir = None  # type: ignore[assignment]
        # Held so cancel() can flip the session's stop_event directly,
        # instead of waiting for the next per-page progress callback to
        # relay self._cancelled.
        self._session = None  # type: ignore[assignment]

    # ------------------------------------------------------------ API

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("scan already running")
        self._thread = threading.Thread(
            target=self._run, name="gui-scan-runner", daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """Mark cancellation + immediately notify the capture session.

        Setting the session's stop_event here (rather than waiting for
        the next per-page progress callback to relay self._cancelled)
        cuts the cancel latency from ~2-4s to <1s. Producer notices on
        its next ``_stop_event.wait()`` and bails out of the swipe loop,
        the consumer drops the queue and exits. ADB disconnect then
        happens via this thread's ``finally`` block in ``_run``.
        """
        self._cancelled = True
        sess = self._session
        if sess is not None:
            try:
                sess._stop_event.set()  # noqa: SLF001
            except Exception:
                pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def result(self) -> CaptureResult | None:
        return self._result

    @property
    def session_dir(self):  # type: ignore[no-untyped-def]
        """On-disk folder for this scan (pages + members.json + summary)."""
        return self._session_dir

    @property
    def was_cancelled(self) -> bool:
        return self._cancelled

    # ------------------------------------------------------------ thread body

    def _run(self) -> None:
        try:
            self.events.put(("status", "正在初始化擷取流程…"))
            matcher = Matcher(self.records)
            seen_keys: set[str] = set()
            # CapturedMember objects awaiting fuzzy phase, keyed by
            # dedup_key so the UI can update the right placeholder row.
            pending: dict[str, CapturedMember] = {}
            # Track every emitted LiveMember by dedup_key so the summary
            # carries the final state for each row (resolved or not).
            members_by_key: dict[str, LiveMember] = {}

            # Pull fallback threshold from config.ini if the user
            # configured one; otherwise CaptureSession's own default
            # (0.92 as of 2026-05-22) applies.
            from ..utils.config import app_config
            cfg = app_config()
            session_kwargs: dict = dict(
                adb=self.adb,
                ocr=self.ocr,
                fallback_ocr=self.fallback_ocr,
                max_pages=self.max_pages,
            )
            if cfg.ocr_fallback_threshold is not None:
                session_kwargs["fallback_threshold"] = cfg.ocr_fallback_threshold
            session = CaptureSession(**session_kwargs)
            self._session_dir = session.session_dir
            self._session = session  # exposed to cancel()

            # Wrap CaptureSession._merge so each new dedup row triggers
            # an exact-match attempt the moment it lands. CaptureSession
            # keeps no UI state of its own, so this is the cleanest seam.
            original_merge = session._merge

            def _wrapped_merge(rows, members, page_idx):  # type: ignore[no-untyped-def]
                pre_keys = set(members.keys())
                added = original_merge(rows, members, page_idx)

                # First-page sanity check: if OCR couldn't find a recognisable
                # member list on the very first screencap, the user almost
                # certainly forgot to switch LDPlayer to 公會 → 公會成員 before
                # hitting 開始掃描. Bail out early with a clear message
                # rather than spending two minutes producing an empty file.
                if page_idx == 0:
                    n_rows = len(rows)
                    n_valid_gear = sum(
                        1 for r in rows
                        if r.gear_score and 0 < r.gear_score < 10_000_000
                    )
                    if n_rows < 3 or n_valid_gear == 0:
                        self.events.put(("not_member_list", {
                            "n_rows": n_rows,
                            "n_valid_gear": n_valid_gear,
                        }))
                        # Treat this exactly like a user-pressed cancel —
                        # the app's _handle_summary path will purge the
                        # half-baked session folder afterwards.
                        self._cancelled = True
                        session._stop_event.set()
                        return added

                for key in set(members.keys()) - pre_keys:
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    cm: CapturedMember = members[key]
                    self._dispatch_exact(cm, matcher, pending, members_by_key)
                return added

            session._merge = _wrapped_merge  # type: ignore[assignment]

            def _progress(page: int, total: int, new: int) -> None:
                if self._cancelled:
                    session._stop_event.set()
                self.events.put(("progress", page, total, new))

            def _capture_progress(captured: int, total: int) -> None:
                # Producer thread — push a UI event for the camera
                # progress line ("截圖: N/45"). UI thread handles the
                # actual repaint via the standard event queue.
                self.events.put(("capture_progress", captured, total))

            self.events.put(("status", "開始掃描，請勿操作雷電視窗…"))
            result = session.run(
                progress=_progress, capture_progress=_capture_progress,
            )
            self._result = result

            # Snapshot which records phase A latched onto, so phase B
            # fuzzy hits surface as candidates (NOT pure missed) below.
            exact_matched: set[int] = {
                lm.record_index for lm in members_by_key.values()
                if lm.record_index is not None and lm.decision == "exact"
            }

            # Phase B — phase-3 fuzzy (correct_nickname only).
            self.events.put(("status", "分析截圖完成，正在做模糊比對…"))
            pages_dir = session.pages_dir
            unmatched_items: list[UnmatchedReviewItem] = []
            fuzzy_hits: dict[int, LiveMember] = {}
            for key, cm in pending.items():
                lm = _resolve_fuzzy(cm, matcher, self.records, self.capture_day)
                members_by_key[key] = lm
                self.events.put(("member_resolved", lm))
                if lm.decision == "fuzzy_review" and lm.record_index is not None:
                    fuzzy_hits[lm.record_index] = lm
                elif lm.decision == "unmatched":
                    unmatched_items.append(UnmatchedReviewItem(
                        ocr_nickname=lm.ocr_nickname,
                        gear_score=lm.gear_score,
                        confidence=lm.confidence,
                        page_index=lm.page_index,
                        image_path=pages_dir / f"page_{lm.page_index:03d}.png",
                        row_y=lm.row_y,
                    ))

            missed_items = _build_missed(
                matcher, self.records,
                exact_matched=exact_matched,
                fuzzy_hits=fuzzy_hits,
                pages_dir=pages_dir,
            )

            summary = SummaryPayload(
                capture_day=self.capture_day,
                result=result,
                members=list(members_by_key.values()),
                missed=missed_items,
                unmatched=unmatched_items,
            )
            self.events.put(("summary", summary))
        except BaseException as exc:  # noqa: BLE001
            logger.exception("scan runner crashed")
            self.events.put(("error", exc))
        finally:
            try:
                self.adb.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------ helpers

    def _dispatch_exact(
        self,
        cm: CapturedMember,
        matcher: Matcher,
        pending: dict[str, CapturedMember],
        members_by_key: dict[str, LiveMember],
    ) -> None:
        """Run phase-A exact match and emit either resolved or pending."""
        lm = _resolve_exact(cm, matcher, self.records, self.capture_day)
        if lm is None:
            # Hold for fuzzy. UI gets a placeholder so the row count
            # tracks reality during the scan.
            pending[cm.dedup_key] = cm
            placeholder = LiveMember(
                ocr_nickname=(cm.nickname or "").strip() or "(空)",
                gear_score=cm.gear_score,
                confidence=cm.nickname_confidence,
                matched_to=None,
                previous_gear=None,
                delta=None,
                decision="pending",
                record_index=None,
                page_index=cm.first_seen_page,
                dedup_key=cm.dedup_key,
                row_y=cm.first_seen_y,
            )
            members_by_key[cm.dedup_key] = placeholder
            self.events.put(("member_pending", placeholder))
        else:
            members_by_key[cm.dedup_key] = lm
            self.events.put(("member_resolved", lm))
