"""Wide-table guild-scores workbook (v2 schema).

Column layout (left to right; Chinese headers in Excel, snake_case field
names on :class:`PlayerRecord`):

  1. ID          (player_id)            — user-maintained member number
  2. 遊戲ID      (correct_nickname)     — USER FILLS THIS (highlighted yellow)
  3. 職業        (profession)           — user free-text, display only
  4. Last_OCR_ID (latest_ocr_nickname)  — what OCR said most recently
  5. OCR信心     (confidence)           — OCR confidence (0.00 .. 1.00)
  6. 最高裝評    (peak_gear_score)      — high-water mark (user may back-fill)
  7..N. Per-capture columns — one column per calendar day:

         YYYY-MM-DD   (max gear score seen for this player on that day)

``review_reason`` lives only in memory (drives red-row painting) and is
never written as a column; legacy workbooks that still carry it load fine.
Multiple scans on the same day merge into the same column, keeping the
maximum value. The workbook no longer tracks weekly contribution / weekly
activity nor the legacy `known_aliases` column — both proved unnecessary
in real use and were trimmed down to the minimum the user actually reads.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

# A capture-column header is the calendar day in ISO format. The legacy
# v1 layout used "裝評 YYYY-MM-DD HH:MM" with three sibling metric
# columns; v2 collapses to a single per-day column for gear score.
DATE_COL_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})$")
DATE_RE = DATE_COL_RE  # kept for back-compat with importers

# Header strings shown in Excel column row. Internal field names on
# PlayerRecord stay snake_case English; the dict maps header → field.
ID_HEADER = "ID"
NICK_HEADER = "遊戲ID"               # was "correct_nickname"
PROFESSION_HEADER = "職業"           # new col C, free-text
OCR_NICK_HEADER = "Last_OCR_ID"     # was "latest_ocr_nickname"
OCR_CONF_HEADER = "OCR信心"          # was "confidence"
PEAK_HEADER = "最高裝評"

# Schema (2026-05-23+): review_reason no longer surfaces as a column —
# it's still used in-memory for red-row painting decisions but never
# written to Excel. The user wants a cleaner sheet.
META_COLUMNS: tuple[str, ...] = (
    ID_HEADER,
    NICK_HEADER,
    PROFESSION_HEADER,
    OCR_NICK_HEADER,
    OCR_CONF_HEADER,
    PEAK_HEADER,
)

# Legacy header aliases — accepted on load so a workbook saved by an
# older build still opens. Maps {old/new header} → internal field name.
_HEADER_FIELD_MAP: dict[str, str] = {
    # New canonical headers
    ID_HEADER: "player_id",
    NICK_HEADER: "correct_nickname",
    PROFESSION_HEADER: "profession",
    OCR_NICK_HEADER: "latest_ocr_nickname",
    OCR_CONF_HEADER: "confidence",
    PEAK_HEADER: "peak_gear_score",
    # Legacy aliases (pre-2026-05-23)
    "correct_nickname": "correct_nickname",
    "latest_ocr_nickname": "latest_ocr_nickname",
    "confidence": "confidence",
    "review_reason": "review_reason",  # read-only, never written back
}

USER_FILL_COLUMN = "correct_nickname"

REVIEW_LOW_CONF = "low_ocr_confidence"
REVIEW_MISSING_NICK = "missing_nickname"
REVIEW_NEW = "new_member"
REVIEW_UNMATCHED = "unmatched_after_capture"
# Existing Excel row that didn't show up in the latest capture. The user
# already filled correct_nickname so the row is real; they may want to
# either fill the metric columns manually, or wait for next scan, or
# investigate why the row was missed. Painted red like REVIEW_NEW so the
# user notices it at a glance.
REVIEW_MISSED_THIS = "missed_this_capture"

# Fuzzy-matched rows always need a human eyeball — they're best-effort,
# not user-confirmed truth. Painting them red (same wash as NEW / MISSED)
# makes the workbook self-document which rows the next scan should
# verify before trusting.
_RED_ROW_REASONS = frozenset({REVIEW_NEW, REVIEW_MISSED_THIS, REVIEW_UNMATCHED})


_PUNCT_RE = re.compile(
    r"[\s　\.\,\;\:\!\?\-\—\–\_\(\)\[\]\{\}\<\>\@\#\$\%\^\&\*\+\=\|\\/"
    r"\"\'`~、。！，：；？「」『』"
    r"‘’“”…©®™★☆♥♡"
    r"♪♫〇]+"
)


def _normalize(s: str) -> str:
    """Lower-cased, punctuation-stripped, NFKC-normalised string for matching."""
    if not s:
        return ""
    n = unicodedata.normalize("NFKC", s)
    n = _PUNCT_RE.sub("", n)
    return n.casefold()


def _to_date_label(label: str) -> str:
    """Normalise any capture label down to ``YYYY-MM-DD``.

    Callers occasionally hand us a v1-style ``YYYY-MM-DD HH:MM`` from old
    code paths; we just drop the time portion. Anything else raises so the
    bug surfaces early.
    """
    label = (label or "").strip()
    if DATE_COL_RE.match(label):
        return label
    # Accept v1 "YYYY-MM-DD HH:MM" by dropping the time.
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+\d{2}:\d{2})?$", label)
    if m:
        return m.group(1)
    raise ValueError(f"capture_label must be YYYY-MM-DD, got {label!r}")


@dataclass
class MergeMatch:
    gear: int
    ocr_nickname: str
    matched_to: str
    # "exact_correct" | "exact_ocr" | "fuzzy_correct" | "fuzzy_ocr"
    # | "new" | "missed"
    match_via: str
    score: float | None
    # Top runners-up from the matcher, formatted like "alice@88".
    alternatives: str = ""
    # Gear score this player had on the most recent previous day with
    # a recorded value (None if they have no prior record). Used by the
    # GUI to display "12000 (+2000)" change indicators in real time.
    previous_gear: int | None = None
    # Index into ``GuildScoresWorkbook.records`` for the touched row.
    # ``None`` for "new" matches that weren't committed. Lets the GUI's
    # review dialog wire user-typed gear values back to the correct row.
    record_index: int | None = None
    # Page index of the original screenshot for unmatched captures —
    # so the review dialog can point at ``page_009.png`` etc.
    page_index: int | None = None

    @property
    def delta(self) -> int | None:
        if self.previous_gear is None:
            return None
        return self.gear - self.previous_gear


@dataclass
class FuzzyCandidate:
    """A phase-3 fuzzy match the user needs to approve before we write it.

    Surfaced in the post-scan review dialog's ❶ section: each candidate
    pairs a workbook row (record_index + record_name) with the captured
    OCR data that fuzzy-matched it. If the user ticks 套用, the gear
    score lands in the day's column AND ``latest_ocr_nickname`` updates
    to the OCR string so future scans can exact-match the same string.
    """
    record_index: int
    record_name: str
    ocr_nickname: str
    gear_score: int
    confidence: float | None
    score: float
    page_index: int | None = None
    row_y: int | None = None


@dataclass
class MergeResult:
    """Outcome of a single :meth:`GuildScoresWorkbook.merge_capture` call.

    * ``updated``: every capture that *was* applied to an Excel row,
      tagged with how the match was made.
    * ``unmatched_captured``: every captured row that could not be paired
      with any row in the workbook (score below review threshold). These
      become fresh red-row entries appended at the bottom.
    * ``missed_in_capture``: every existing workbook row that the OCR
      pass did not see this round. Tagged for review but not modified
      otherwise.
    """

    updated: list[MergeMatch] = field(default_factory=list)
    unmatched_captured: list[MergeMatch] = field(default_factory=list)
    missed_in_capture: list[MergeMatch] = field(default_factory=list)
    # Phase-3 fuzzy hits that were NOT applied to the workbook —
    # waiting for the user's explicit approval via the review dialog.
    fuzzy_candidates: list[FuzzyCandidate] = field(default_factory=list)

    @property
    def added(self) -> list[MergeMatch]:
        return self.unmatched_captured

    @property
    def n_exact_correct(self) -> int:
        return sum(1 for m in self.updated if m.match_via == "exact_correct")

    @property
    def n_exact_ocr(self) -> int:
        return sum(1 for m in self.updated if m.match_via == "exact_ocr")

    @property
    def n_fuzzy_correct(self) -> int:
        return sum(1 for m in self.updated if m.match_via == "fuzzy_correct")

    @property
    def n_fuzzy_ocr(self) -> int:
        return sum(1 for m in self.updated if m.match_via == "fuzzy_ocr")

    @property
    def n_new(self) -> int:
        return len(self.unmatched_captured)


def _parse_capture_header(header: str) -> str | None:
    """Return the date string if ``header`` is a capture column, else None."""
    if not isinstance(header, str):
        return None
    m = DATE_COL_RE.match(header.strip())
    return m.group("date") if m else None


@dataclass
class PlayerRecord:
    # Stable per-player identifier shown in column A. Assigned once
    # (sequentially during load() backfill or phase-5 append) and then
    # preserved through every subsequent merge / rename — survives
    # renaming, scan misses, everything but explicit deletion. ``None``
    # in memory means "pending backfill on next save".
    player_id: int | None = None
    correct_nickname: str = ""
    # User-maintained free-text job class (e.g. "騎士", "巫師"). Used by
    # the gear-distribution chart to colour-classify; not consulted by
    # any matching logic. Preserved verbatim through merge / rename.
    profession: str = ""
    latest_ocr_nickname: str = ""
    confidence: float | None = None
    review_reason: str = ""
    # Persistent peak gear score. Used to be a @property computed from
    # gear_scores, but the user often back-fills this column manually
    # with values from days that predate the visible date columns — we
    # must respect those entries. New captures may only *bump* this
    # upward (record_gear_for_day enforces the rule); a new capture
    # that comes in below the stored peak does NOT overwrite it.
    # ``None`` = no peak recorded yet.
    peak_gear_score: int | None = None
    # Keyed by capture day "YYYY-MM-DD" → max gear score seen that day.
    gear_scores: dict[str, int] = field(default_factory=dict)

    def previous_gear_before(self, day: str) -> int | None:
        """Return the most recent recorded gear-score strictly before ``day``.

        Used by the live UI to compute "12000 (+2000)" deltas as captures
        roll in. Days with no recorded value are skipped.
        """
        prior_days = sorted(d for d in self.gear_scores if d < day)
        if not prior_days:
            return None
        return self.gear_scores[prior_days[-1]]

    def last_recorded_day(self) -> str | None:
        """Most recent day with a gear value, or None for new players."""
        if not self.gear_scores:
            return None
        return sorted(self.gear_scores.keys())[-1]

    def record_gear_for_day(self, day: str, gear: int) -> int:
        """Store ``gear`` into ``day``, keeping the maximum if a value already exists.

        Also bumps ``peak_gear_score`` only when ``gear`` exceeds the
        stored peak — the peak is treated as a high-water mark that
        includes pre-history values the user typed in by hand, so a
        smaller capture must not overwrite it.

        Returns whatever ended up stored for the day (caller can use
        this to detect whether the capture actually shifted the value
        upward).
        """
        existing = self.gear_scores.get(day)
        new_val = gear if existing is None else max(existing, gear)
        self.gear_scores[day] = new_val
        if self.peak_gear_score is None or new_val > self.peak_gear_score:
            self.peak_gear_score = new_val
        return new_val


def duplicate_nickname_issues(records: "Iterable[PlayerRecord]") -> list[str]:
    """Detect duplicate 遊戲ID values (post match-normalisation).

    The matcher's exact lookup is first-filled-wins, so of two rows
    sharing a 遊戲ID only the first can ever be matched — the other is
    silently marked 未掃到 every scan. Mirrors the league side's
    ``roster_issues``: surface the problem at load time so the user
    fixes the workbook instead of chasing phantom misses.
    """
    from ..matching.matcher import normalize_for_match

    issues: list[str] = []
    seen: dict[str, str] = {}
    for rec in records:
        name = rec.correct_nickname.strip()
        if not name:
            continue
        norm = normalize_for_match(name)
        if not norm:
            continue
        if norm in seen:
            issues.append(
                f"遊戲ID 重複：「{seen[norm]}」與「{name}」"
                "（掃描只會配對到前者，後者每次都會被標成未掃到）"
            )
        else:
            seen[norm] = name
    return issues


class GuildScoresWorkbook:
    """In-memory model of the wide-table workbook (v2 schema)."""

    SHEET_NAME = "Members"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.records: list[PlayerRecord] = []
        # Sorted list of capture days (oldest first), e.g. "2026-05-18".
        self.capture_days: list[str] = []

    # Back-compat shim so callers that still reach for the old name keep
    # working — every internal use has been migrated to `capture_days`.
    @property
    def capture_timestamps(self) -> list[str]:
        return self.capture_days

    # ----------------------------------------------------------- load / save

    @classmethod
    def load(cls, path: Path) -> "GuildScoresWorkbook":
        wb = cls(path)
        if not wb.path.is_file():
            return wb
        book = load_workbook(wb.path, data_only=False)
        # Prefer the canonical "Members" sheet. Fall back to whichever
        # sheet is first when a user hand-crafts a workbook with just a
        # ``correct_nickname`` column under any name (e.g. "Sheet1");
        # save() will normalise the sheet back to "Members" on the next
        # write so the workbook self-heals after one round-trip.
        if cls.SHEET_NAME in book.sheetnames:
            ws = book[cls.SHEET_NAME]
        elif book.sheetnames:
            ws = book[book.sheetnames[0]]
        else:
            return wb
        headers = [cell.value for cell in ws[1]]
        col_index = {h: i for i, h in enumerate(headers) if h}

        # Map each capture column index to its day.
        capture_cols: list[tuple[int, str]] = []  # (col_index, day)
        for h, idx in col_index.items():
            day = _parse_capture_header(str(h))
            if day:
                capture_cols.append((idx, day))
        wb.capture_days = sorted({d for _, d in capture_cols})

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not any(cell is not None and str(cell).strip() for cell in row):
                continue

            def get(*names: str) -> object | None:
                """Return the first column value matching any of ``names``.

                Accepts both new (Chinese) and legacy (English) header
                names so workbooks written by older builds still load.
                """
                for n in names:
                    idx = col_index.get(n)
                    if idx is not None and idx < len(row):
                        return row[idx]
                return None

            confidence = get(OCR_CONF_HEADER, "confidence")
            try:
                conf_val = float(confidence) if confidence not in (None, "") else None
            except (TypeError, ValueError):
                conf_val = None
            id_raw = get(ID_HEADER)
            try:
                pid = int(id_raw) if id_raw not in (None, "") else None
            except (TypeError, ValueError):
                pid = None
            peak_raw = get(PEAK_HEADER)
            try:
                peak_val = int(peak_raw) if peak_raw not in (None, "") else None
            except (TypeError, ValueError):
                peak_val = None
            record = PlayerRecord(
                player_id=pid,
                correct_nickname=str(get(NICK_HEADER, "correct_nickname") or "").strip(),
                profession=str(get(PROFESSION_HEADER) or "").strip(),
                latest_ocr_nickname=str(get(OCR_NICK_HEADER, "latest_ocr_nickname") or "").strip(),
                confidence=conf_val,
                # review_reason no longer surfaces as a column in the
                # new schema, but legacy workbooks still have it — read
                # it if present so red-row flags survive a load+save.
                review_reason=str(get("review_reason") or "").strip(),
                peak_gear_score=peak_val,
            )
            for idx, day in capture_cols:
                if idx >= len(row):
                    continue
                cell = row[idx]
                if cell is None or cell == "":
                    continue
                try:
                    record.gear_scores[day] = int(cell)
                except (TypeError, ValueError):
                    continue
            # Reconcile: peak must be ≥ everything visible in the date
            # columns. The user-typed peak is treated as a high-water
            # mark from pre-history, but if a date column already shows
            # something higher than that, trust the date column.
            if record.gear_scores:
                day_max = max(record.gear_scores.values())
                if record.peak_gear_score is None or day_max > record.peak_gear_score:
                    record.peak_gear_score = day_max
            wb.records.append(record)
        return wb

    def save(self, *, backup: bool = True, backup_dir: Path | None = None) -> Path | None:
        backup_path: Path | None = None
        if backup and self.path.is_file():
            backup_path = self._make_backup(backup_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ID column is read-only — we write back whatever the user set
        # in column A (or leave blank if a phase-5 append still hasn't
        # been numbered). The code never invents, shifts, or compacts
        # IDs; the user owns column A by hand. New phase-5 rows stay
        # appended in capture order with no auto-shifting.
        book = Workbook()
        ws = book.active
        ws.title = self.SHEET_NAME
        self._write_to_sheet(ws)
        book.save(self.path)
        return backup_path

    # ----------------------------------------------------------- manual missed entries
    #
    # After the post-scan review dialog, the user may have typed gear
    # values for rows that the scan didn't see. We commit those here.
    # Anything they left blank is silently skipped — exactly matches the
    # "沒填也沒關係" branch in the spec.

    def apply_review_decisions(
        self,
        capture_label: str,
        manual_gear: dict[int, int | None],
        approved_fuzzy: list[FuzzyCandidate],
    ) -> dict[str, int]:
        """Commit the user's choices from the review dialog.

        Two flavours of write:

          * ``manual_gear`` — the user typed a gear value into the
            "Excel 有但本次沒掃到" row's input box. Writes gear only;
            ``latest_ocr_nickname`` untouched (we have no OCR data for
            this row this round).
          * ``approved_fuzzy`` — the user ticked 套用 on a phase-3
            candidate. Writes gear AND updates the row's
            ``latest_ocr_nickname`` / ``confidence`` to the captured
            values, so the next scan exact-matches the same OCR string.

        Returns a counts dict ``{"manual": N, "fuzzy": M}``. Adds the
        day to ``capture_days`` if either flavour wrote anything.
        """
        day = _to_date_label(capture_label)
        n_manual = 0
        n_fuzzy = 0

        for record_idx, gear in manual_gear.items():
            if gear is None:
                continue
            if not 0 <= record_idx < len(self.records):
                continue
            rec = self.records[record_idx]
            rec.record_gear_for_day(day, int(gear))
            if rec.review_reason == REVIEW_MISSED_THIS:
                rec.review_reason = ""
            n_manual += 1

        for cand in approved_fuzzy:
            if not 0 <= cand.record_index < len(self.records):
                continue
            rec = self.records[cand.record_index]
            rec.latest_ocr_nickname = cand.ocr_nickname
            rec.confidence = cand.confidence
            rec.record_gear_for_day(day, cand.gear_score)
            # User explicitly approved this match — drop any review flag.
            # Next scan should exact-match against the new latest_ocr_nickname.
            if rec.review_reason in {
                REVIEW_LOW_CONF, REVIEW_MISSING_NICK, REVIEW_NEW,
                REVIEW_UNMATCHED, REVIEW_MISSED_THIS,
            }:
                rec.review_reason = ""
            n_fuzzy += 1

        if (n_manual or n_fuzzy) and day not in self.capture_days:
            self.capture_days.append(day)
            self.capture_days.sort()
        return {"manual": n_manual, "fuzzy": n_fuzzy}

    # ----------------------------------------------------------- rename
    #
    # Triggered from the GUI when the user wants to fix a row whose OCR
    # output is wrong (or where the in-game player has actually renamed
    # themselves). We commit the new ``correct_nickname`` and wipe the
    # OCR snapshot so the next scan starts fresh on this row. Gear-score
    # history is preserved unconditionally — that's the whole point of
    # the workbook.

    def rename_player(
        self,
        record_index: int,
        new_nickname: str,
    ) -> "PlayerRecord":
        if not 0 <= record_index < len(self.records):
            raise IndexError(
                f"record_index {record_index} out of range "
                f"(have {len(self.records)} records)"
            )
        name = (new_nickname or "").strip()
        if not name:
            raise ValueError("new_nickname cannot be empty")
        rec = self.records[record_index]
        rec.correct_nickname = name
        # Reset OCR snapshot — caller wants the next scan to re-establish
        # what OCR currently sees for this player.
        rec.latest_ocr_nickname = ""
        rec.confidence = None
        # Clear "needs review" markers tied to OCR uncertainty; the user
        # has just told us the truth.
        if rec.review_reason in {
            REVIEW_LOW_CONF,
            REVIEW_MISSING_NICK,
            REVIEW_NEW,
            REVIEW_UNMATCHED,
        }:
            rec.review_reason = ""
        return rec

    def _make_backup(self, backup_dir: Path | None) -> Path:
        target_dir = backup_dir or (self.path.parent / "backups")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = target_dir / f"{self.path.stem}_{stamp}{self.path.suffix}"
        shutil.copy2(self.path, out)
        return out

    # ----------------------------------------------------------- bootstrap

    def bootstrap_from_captures(
        self,
        captured: Iterable[dict],
        capture_label: str,
        *,
        low_conf_threshold: float = 0.80,
    ) -> None:
        """Populate ``self.records`` from a list of captured-member dicts.

        ``capture_label`` is the calendar day "YYYY-MM-DD". The list is
        preserved in original capture order so the workbook scrolls in the
        same direction as the in-game member list — easier for users to
        cross-check by eye.
        """
        day = _to_date_label(capture_label)
        members = sorted(
            captured,
            key=lambda m: (
                m.get("first_seen_page", 0),
                m.get("first_seen_y", 0),
            ),
        )
        self.records = []
        for m in members:
            nickname = (m.get("nickname") or "").strip()
            confidence = m.get("nickname_confidence")
            review = ""
            if not nickname:
                review = REVIEW_MISSING_NICK
            elif confidence is not None and confidence < low_conf_threshold:
                review = REVIEW_LOW_CONF
            gear = int(m["gear_score"])
            record = PlayerRecord(
                correct_nickname="",  # USER FILLS THIS
                latest_ocr_nickname=nickname,
                confidence=confidence,
                review_reason=review,
                # Fresh bootstrap → peak == this capture, by definition.
                peak_gear_score=gear,
                gear_scores={day: gear},
            )
            self.records.append(record)
        if day not in self.capture_days:
            self.capture_days.append(day)
            self.capture_days.sort()

    # --------------------------------------------------- merge (round-trip)

    def merge_capture(
        self,
        captured: Iterable[dict],
        capture_label: str,
        *,
        low_conf_threshold: float = 0.80,
        append_unmatched: bool = True,
        mark_missed: bool = True,
    ) -> "MergeResult":
        """Graft a new capture onto the existing records non-destructively.

        **Invariants**:

          1. ``self.records`` order is never reshuffled.
          2. Existing records are never removed.
          3. ``correct_nickname`` is never overwritten.
          4. Same-day gear-score cell keeps the **maximum** of the existing
             and new value (user requested "one entry per day, take max").

        **Flags**:

          * ``append_unmatched`` — when True (legacy default) phase-5
            captures are appended as fresh red rows. When False they are
            collected into ``MergeResult.unmatched_captured`` only, so
            the caller can show the user a confirmation dialog before
            committing.
          * ``mark_missed`` — when True (legacy default) any existing
            record not seen this round gets ``review_reason=REVIEW_MISSED_THIS``
            (red row). When False the rows are still surfaced in
            ``missed_in_capture`` for the caller's UI, but no write occurs.

        **Four-phase matching** (each phase only sees records not yet
        claimed):

          Phase 1 — exact match against ``correct_nickname`` (user truth).
          Phase 2 — exact match against ``latest_ocr_nickname``.
          Phase 3 — fuzzy match against ``correct_nickname`` (≥ 65).
          Phase 4 — fuzzy match against ``latest_ocr_nickname`` (≥ 65).

        Per the rewritten UX, **all fuzzy matches** (Phase 3 / 4) are
        flagged with ``REVIEW_UNMATCHED`` regardless of score — they
        must be reviewed by a human before being trusted, so the writer
        paints them red.
        """
        from ..matching import Matcher

        day = _to_date_label(capture_label)
        captured_list = list(captured)
        matcher = Matcher(self.records)
        result = MergeResult()

        # --- Phase 1: exact correct_nickname --------------------------------
        leftover: list[dict] = []
        for m in captured_list:
            nickname = (m.get("nickname") or "").strip()
            idx = matcher.exact_match_correct(nickname)
            if idx is None:
                leftover.append(m)
                continue
            matcher.mark_claimed(idx)
            match = self._apply_capture_to_record(
                idx, m, day,
                low_conf_threshold=low_conf_threshold,
                match_via="exact_correct", match_score=100.0,
                alternatives="",
            )
            result.updated.append(match)

        # --- Phase 2: exact latest_ocr_nickname -----------------------------
        next_leftover: list[dict] = []
        for m in leftover:
            nickname = (m.get("nickname") or "").strip()
            idx = matcher.exact_match_ocr(nickname)
            if idx is None:
                next_leftover.append(m)
                continue
            matcher.mark_claimed(idx)
            match = self._apply_capture_to_record(
                idx, m, day,
                low_conf_threshold=low_conf_threshold,
                match_via="exact_ocr", match_score=100.0,
                alternatives="",
            )
            result.updated.append(match)
        leftover = next_leftover

        # --- Phase 3: fuzzy correct_nickname — COLLECT, don't apply --------
        # Per the 2026-05-22 rewrite, phase-3 hits no longer auto-apply.
        # Instead we collect them as candidates the review dialog will
        # surface with an explicit "套用" checkbox. The matcher still
        # marks them as claimed so two captures can't fuzzy-match the
        # same record. If the user rejects in the dialog, the workbook
        # row stays untouched.
        next_leftover = []
        for m in leftover:
            nickname = (m.get("nickname") or "").strip()
            cand = matcher.fuzzy_match_correct(nickname)
            if cand is None:
                next_leftover.append(m)
                continue
            matcher.mark_claimed(cand.record_index)
            rec = self.records[cand.record_index]
            result.fuzzy_candidates.append(FuzzyCandidate(
                record_index=cand.record_index,
                record_name=rec.correct_nickname or rec.latest_ocr_nickname or "",
                ocr_nickname=nickname,
                gear_score=int(m.get("gear_score") or 0),
                confidence=m.get("nickname_confidence"),
                score=cand.score,
                page_index=m.get("first_seen_page"),
                row_y=m.get("first_seen_y"),
            ))
        leftover = next_leftover

        # --- Phase 4 (fuzzy_match_ocr) REMOVED 2026-05-22 ------------------
        # OCR-side fuzzy matching produced noisy false positives —
        # collapsed to just phase-3 (fuzzy against user-curated truth).

        # --- Phase 5: handle captures that matched nothing ------------------
        # ``append_unmatched`` flips behaviour: True (legacy) appends red
        # rows; False just collects them so the caller can show a review
        # dialog and decide per-row.
        for m in leftover:
            nickname = (m.get("nickname") or "").strip()
            confidence = m.get("nickname_confidence")
            gear_val = int(m.get("gear_score") or 0)
            if append_unmatched:
                # New members get NO auto-ID — the user fills column A
                # by hand later, typically reusing the row number of a
                # departed guild member rather than burning a fresh one.
                new_rec = PlayerRecord(
                    correct_nickname="",
                    latest_ocr_nickname=nickname,
                    confidence=confidence,
                    review_reason=REVIEW_NEW,
                    peak_gear_score=gear_val,
                )
                new_rec.gear_scores[day] = gear_val
                self.records.append(new_rec)
            result.unmatched_captured.append(
                MergeMatch(
                    gear=gear_val,
                    ocr_nickname=nickname,
                    matched_to="(appended as new)" if append_unmatched else "(not committed)",
                    match_via="new",
                    score=None,
                    alternatives="",
                    previous_gear=None,
                    record_index=None,
                    page_index=m.get("first_seen_page"),
                )
            )

        # --- Records that didn't appear in this capture --------------------
        for i, rec in enumerate(self.records):
            if matcher.is_claimed(i):
                continue
            if not rec.gear_scores and not rec.correct_nickname:
                continue
            # Skip rows just appended in phase 5 (they weren't in the matcher).
            if rec.review_reason == REVIEW_NEW and day in rec.gear_scores:
                continue
            if mark_missed:
                rec.review_reason = REVIEW_MISSED_THIS
            result.missed_in_capture.append(
                MergeMatch(
                    gear=rec.peak_gear_score or 0,
                    ocr_nickname=rec.correct_nickname
                    or rec.latest_ocr_nickname,
                    matched_to="(not in this capture)",
                    match_via="missed",
                    score=None,
                    alternatives="",
                    previous_gear=None,
                    record_index=i,
                    page_index=None,
                )
            )

        if day not in self.capture_days:
            self.capture_days.append(day)
            self.capture_days.sort()
        return result

    # -------------------------------------------------- merge helper

    def _apply_capture_to_record(
        self,
        record_index: int,
        captured: dict,
        day: str,
        *,
        low_conf_threshold: float,
        match_via: str,
        match_score: float | None,
        alternatives: str,
        force_review: bool = False,
    ) -> "MergeMatch":
        """Apply a captured row's data to an existing record in place.

        ``correct_nickname`` is never touched — it is the user's truth.
        The day's gear-score cell keeps the maximum of any existing value
        and the new capture.
        """
        rec = self.records[record_index]
        nickname = (captured.get("nickname") or "").strip()
        confidence = captured.get("nickname_confidence")
        gear = int(captured.get("gear_score") or 0)
        previous_gear = rec.previous_gear_before(day)

        rec.latest_ocr_nickname = nickname
        rec.confidence = confidence
        rec.record_gear_for_day(day, gear)

        review = ""
        if force_review:
            review = REVIEW_UNMATCHED
        elif not nickname:
            review = REVIEW_MISSING_NICK
        elif confidence is not None and confidence < low_conf_threshold:
            review = REVIEW_LOW_CONF
        rec.review_reason = review

        return MergeMatch(
            gear=gear,
            ocr_nickname=nickname,
            matched_to=rec.correct_nickname or rec.latest_ocr_nickname,
            match_via=match_via,
            score=match_score,
            alternatives=alternatives,
            previous_gear=previous_gear,
            record_index=record_index,
            page_index=captured.get("first_seen_page"),
        )

    # ----------------------------------------------------------- sheet writing

    def _write_to_sheet(self, ws: Worksheet) -> None:
        headers = list(META_COLUMNS) + list(self.capture_days)
        ws.append(headers)

        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="305496")
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        thin = Side(border_style="thin", color="BFBFBF")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        for col_idx, _ in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = border

        user_fill = PatternFill("solid", fgColor="FFF2CC")  # yellow
        ocr_fill = PatternFill("solid", fgColor="F2F2F2")     # light grey
        peak_fill = PatternFill("solid", fgColor="E2EFDA")    # light green
        new_row_fill = PatternFill("solid", fgColor="FFC7CE")  # light red
        gear_fill = PatternFill("solid", fgColor="DDEBF7")    # blue

        # Per 2026-05-23 spec: every cell horizontal-centered.
        cell_center = Alignment(horizontal="center", vertical="center")

        for r_idx, record in enumerate(self.records, start=2):
            is_red_row = record.review_reason in _RED_ROW_REASONS
            # ``review_reason`` no longer surfaces as its own column —
            # row order here matches META_COLUMNS exactly.
            row_values = [
                record.player_id,
                record.correct_nickname,
                record.profession,
                record.latest_ocr_nickname,
                round(record.confidence, 3) if record.confidence is not None else None,
                record.peak_gear_score,
            ]
            for day in self.capture_days:
                row_values.append(record.gear_scores.get(day, None))
            for c_idx, value in enumerate(row_values, start=1):
                cell = ws.cell(row=r_idx, column=c_idx, value=value)
                cell.border = border
                cell.alignment = cell_center
                header = headers[c_idx - 1]
                if is_red_row:
                    cell.fill = new_row_fill
                    if header == PEAK_HEADER or _parse_capture_header(header):
                        if value is not None:
                            cell.number_format = "#,##0"
                    elif header == OCR_CONF_HEADER and value is not None:
                        cell.number_format = "0.000"
                    continue
                if header == NICK_HEADER:
                    cell.fill = user_fill
                elif header == PROFESSION_HEADER:
                    # Same yellow tint as 遊戲ID so the two user-curated
                    # columns visually group.
                    cell.fill = user_fill
                elif header == OCR_NICK_HEADER:
                    cell.fill = ocr_fill
                elif header == PEAK_HEADER:
                    cell.fill = peak_fill
                    if value is not None:
                        cell.number_format = "#,##0"
                else:
                    if _parse_capture_header(header) is not None:
                        if value is not None:
                            cell.number_format = "#,##0"
                        cell.fill = gear_fill
                    elif header == OCR_CONF_HEADER and value is not None:
                        cell.number_format = "0.000"

        widths = {
            ID_HEADER: 6,
            NICK_HEADER: 22,
            PROFESSION_HEADER: 14,
            OCR_NICK_HEADER: 22,
            OCR_CONF_HEADER: 13,
            PEAK_HEADER: 16,
        }
        # "YYYY-MM-DD" needs ~12 chars.
        capture_col_width = 13
        for i, name in enumerate(headers, start=1):
            letter = get_column_letter(i)
            if _parse_capture_header(name) is not None:
                ws.column_dimensions[letter].width = capture_col_width
            else:
                ws.column_dimensions[letter].width = widths.get(name, 12)

        ws.freeze_panes = "C2"
        last_col = get_column_letter(len(headers))
        last_row = len(self.records) + 1
        ws.auto_filter.ref = f"A1:{last_col}{last_row}"
