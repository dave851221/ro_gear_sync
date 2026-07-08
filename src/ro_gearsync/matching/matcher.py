"""Match a freshly-captured OCR nickname against a user's curated roster.

The matcher is the heart of M5. It encodes a few hard-won lessons from
real captures of the user's guild:

  * The user-curated ``correct_nickname`` column is the highest-trust
    signal. ``latest_ocr_nickname`` is a useful fallback but it is still
    OCR output and can itself be wrong.
  * Game-build is Traditional Chinese but OCR sometimes returns a
    Simplified glyph for the same character. We collapse both sides to
    Simplified via OpenCC before scoring so 「瘋狂暗魔陰亮」 and 「疯狂暗魔陰亮」
    are recognised as the same string before fuzzing.
  * OCR also makes shape-similar substitutions across writing systems —
    「暗→喑」、「魔→磨」、「品→呂」. These are NOT covered by Trad↔Simp
    conversion. We accept a lower fuzzy threshold (65) for these cases
    because the cost of a missed match (user re-resolves later) is small
    next to the cost of refusing a 4/6-character match.
  * Within one capture we never match two captured rows to the same
    record — the caller passes the running `claimed` set.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence, TYPE_CHECKING

from opencc import OpenCC
from rapidfuzz import fuzz

if TYPE_CHECKING:
    from ..storage.excel import PlayerRecord


# ---------------------------------------------------------- normalisation


_PUNCT_RE = re.compile(
    r"[\s　\.\,\;\:\!\?\-\—\–\_\(\)\[\]\{\}\<\>\@\#\$\%\^\&\*\+\=\|\\/"
    r"\"\'`~、。！，：；？「」『』"
    r"‘’“”…©®™★☆♥♡"
    r"♪♫〇○●◇◆□■△▲▽▼]+"
)


# Cached at module level — OpenCC's first-time setup parses a 300-ish KB
# config + dict pair, so we want exactly one instance per process.
_T2S = OpenCC("t2s")


def normalize_for_match(s: str) -> str:
    """Apply the full match-time normalisation pipeline.

    Steps:
      1. NFKC (collapses full-width / half-width and compat forms)
      2. Strip whitespace + decorative punctuation
      3. Casefold (handles Latin case)
      4. Traditional → Simplified (so OCR variants reconcile)
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _PUNCT_RE.sub("", s)
    s = s.casefold()
    s = _T2S.convert(s)
    return s


# ---------------------------------------------------------- decision tiers


# Score >= MATCH_AUTO_HIGH: trust completely (essentially exact match).
MATCH_AUTO_HIGH = 95.0
# MATCH_AUTO_LOW <= score < AUTO_HIGH: auto-match, but log so we can audit.
MATCH_AUTO_LOW = 80.0
# MATCH_REVIEW <= score < AUTO_LOW: suggest top candidates, let the user pick.
MATCH_REVIEW = 65.0
# Below MATCH_REVIEW: treat as a new player; the captured row carries no
# strong evidence it's anyone existing.


class MatchDecision(str, Enum):
    AUTO_HIGH = "auto_high"   # ≥ 95
    AUTO_LOW = "auto_low"     # 80–94
    REVIEW = "review"         # 65–79  — surface alternatives, ask the user
    NEW = "new"               # < 65   — treat as a brand-new member


@dataclass
class MatchCandidate:
    """One scoring outcome between a captured nickname and an Excel record."""

    record_index: int
    correct_nickname: str
    matched_field: str       # "correct_nickname" | "latest_ocr_nickname"
    matched_value: str
    score: float

    def __repr__(self) -> str:  # pragma: no cover — debugging aid only
        return (
            f"<MatchCandidate idx={self.record_index} "
            f"{self.matched_field}={self.matched_value!r} score={self.score:.1f}>"
        )


@dataclass
class MatchResult:
    captured_nickname: str
    decision: MatchDecision
    best: MatchCandidate | None
    alternatives: list[MatchCandidate] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.best.score if self.best else 0.0


# ---------------------------------------------------------- the matcher


class Matcher:
    """Match captured OCR nicknames against existing Excel records.

    Lifecycle: build once per merge with the records, then call
    :py:meth:`match` for every captured row. Records that have already
    been claimed (via :py:meth:`mark_claimed`) won't be picked again in
    the same session.
    """

    # Multiplier applied to scores that come from ``correct_nickname`` —
    # we prefer matching against user-validated truth over against
    # another OCR snapshot.
    _CORRECT_BOOST = 1.05
    # When two records tie on score, the higher-trust field wins.
    _FIELD_TRUST = {"correct_nickname": 3, "latest_ocr_nickname": 1}

    def __init__(
        self,
        records: Sequence["PlayerRecord"],
        *,
        review_threshold: float = MATCH_REVIEW,
        auto_threshold: float = MATCH_AUTO_LOW,
        high_threshold: float = MATCH_AUTO_HIGH,
        # Extra OCR strings per record index, matched exactly like
        # ``latest_ocr_nickname``. The league roster stores several
        # historical OCR variants per member (「A｜B｜C」) — pass them
        # split so every variant is an exact/fuzzy key. Gear callers
        # simply omit this.
        ocr_aliases: dict[int, Sequence[str]] | None = None,
        # When False, the records' raw ``latest_ocr_nickname`` value is
        # NOT registered in the pools — only the ``ocr_aliases`` variants
        # are. The league roster packs several ｜-separated variants into
        # that one cell; registering the raw cell would add a junk
        # concatenated key (「A｜B｜C」 normalises to "abc" glued together,
        # since NFKC turns ｜ into | which the punctuation strip removes)
        # that can only ever false-match. Gear callers keep the default —
        # their cell holds a single value.
        use_latest_ocr_field: bool = True,
    ) -> None:
        self.records = list(records)
        self.review_threshold = review_threshold
        self.auto_threshold = auto_threshold
        self.high_threshold = high_threshold
        self._claimed: set[int] = set()

        # Pre-compute normalised forms once. Each entry is
        # (record_index, field_name, original_value, normalised_value).
        self._pool: list[tuple[int, str, str, str]] = []
        # Per-field exact lookups: normalised string → record_index. The
        # workbook should not contain duplicate values within a field;
        # if it does, first-filled wins.
        self._correct_exact: dict[str, int] = {}
        self._ocr_exact: dict[str, int] = {}
        # Per-field fuzzy pools: list of (record_index, normalised string).
        self._correct_strings: list[tuple[int, str]] = []
        self._ocr_strings: list[tuple[int, str]] = []
        for i, rec in enumerate(self.records):
            self._add_to_pool(i, "correct_nickname", rec.correct_nickname)
            if use_latest_ocr_field:
                self._add_to_pool(i, "latest_ocr_nickname", rec.latest_ocr_nickname)
            if rec.correct_nickname:
                cn_norm = normalize_for_match(rec.correct_nickname)
                if cn_norm:
                    self._correct_exact.setdefault(cn_norm, i)
                    self._correct_strings.append((i, cn_norm))
            if use_latest_ocr_field and rec.latest_ocr_nickname:
                ocr_norm = normalize_for_match(rec.latest_ocr_nickname)
                if ocr_norm:
                    self._ocr_exact.setdefault(ocr_norm, i)
                    self._ocr_strings.append((i, ocr_norm))
        if ocr_aliases:
            for i, aliases in ocr_aliases.items():
                if not 0 <= i < len(self.records):
                    continue
                for alias in aliases:
                    norm = normalize_for_match(alias)
                    if not norm:
                        continue
                    self._ocr_exact.setdefault(norm, i)
                    self._ocr_strings.append((i, norm))
                    self._pool.append((i, "latest_ocr_nickname", alias, norm))

    def _add_to_pool(self, idx: int, field_name: str, raw: str) -> None:
        if not raw:
            return
        norm = normalize_for_match(raw)
        if not norm:
            return
        self._pool.append((idx, field_name, raw, norm))

    def mark_claimed(self, record_index: int) -> None:
        self._claimed.add(record_index)

    def is_claimed(self, record_index: int) -> bool:
        return record_index in self._claimed

    def unclaimed_indices(self) -> list[int]:
        return [i for i in range(len(self.records)) if i not in self._claimed]

    # ------------------------------------------------------- phase-1 matching

    def exact_match_correct(self, captured_nickname: str) -> int | None:
        """Phase 1 lookup: normalised-equality against ``correct_nickname``.

        Returns the unclaimed record index or ``None``. Aliases and the OCR
        snapshot are deliberately ignored — phase 1 commits only to fully
        user-validated truth.
        """
        nick_norm = normalize_for_match(captured_nickname)
        if not nick_norm:
            return None
        idx = self._correct_exact.get(nick_norm)
        if idx is None or idx in self._claimed:
            return None
        return idx

    def exact_match_ocr(self, captured_nickname: str) -> int | None:
        """Phase 2 lookup: normalised-equality against
        ``latest_ocr_nickname``.

        When OCR consistently produces the same garbled string for a
        given player (e.g. `0666666` for `__999999`), the previous run's
        recorded OCR snapshot is a perfectly reliable identity key. We
        only consult records that didn't claim a phase-1 slot.
        """
        nick_norm = normalize_for_match(captured_nickname)
        if not nick_norm:
            return None
        idx = self._ocr_exact.get(nick_norm)
        if idx is None or idx in self._claimed:
            return None
        return idx

    # --------------------------------------------------- per-field fuzzy

    def _fuzzy_match_in(
        self,
        captured_nickname: str,
        pool: list[tuple[int, str]],
        field_label: str,
    ) -> MatchCandidate | None:
        """Internal: scan ``pool`` of (record_idx, normalised_string),
        return the best MatchCandidate above ``review_threshold``.
        """
        nick_norm = normalize_for_match(captured_nickname)
        if not nick_norm:
            return None
        best: MatchCandidate | None = None
        for idx, target in pool:
            if idx in self._claimed:
                continue
            score = self._score(nick_norm, target)
            if score < self.review_threshold:
                continue
            if best is None or score > best.score:
                rec = self.records[idx]
                if field_label == "correct_nickname":
                    matched_value = rec.correct_nickname
                else:
                    matched_value = rec.latest_ocr_nickname
                best = MatchCandidate(
                    record_index=idx,
                    correct_nickname=rec.correct_nickname,
                    matched_field=field_label,
                    matched_value=matched_value,
                    score=score,
                )
        return best

    def fuzzy_match_correct(self, captured_nickname: str) -> MatchCandidate | None:
        """Phase 3: multi-scorer fuzzy match against ``correct_nickname``
        of every unclaimed record. Returns the best candidate scoring
        ≥ ``review_threshold`` (default 65), or None.
        """
        return self._fuzzy_match_in(
            captured_nickname, self._correct_strings, "correct_nickname"
        )

    def fuzzy_match_ocr(self, captured_nickname: str) -> MatchCandidate | None:
        """Phase 4: fuzzy match against ``latest_ocr_nickname`` of every
        unclaimed record.
        """
        return self._fuzzy_match_in(
            captured_nickname, self._ocr_strings, "latest_ocr_nickname"
        )

    # ------------------------------------------------------- phase-2 matching

    def match(self, captured_nickname: str) -> MatchResult:
        nick_norm = normalize_for_match(captured_nickname)
        if not nick_norm:
            return MatchResult(
                captured_nickname=captured_nickname,
                decision=MatchDecision.NEW,
                best=None,
                alternatives=[],
            )

        # Score every (record × field) pairing, keep the best per record.
        per_record_best: dict[int, MatchCandidate] = {}
        for idx, field_name, raw, norm in self._pool:
            if idx in self._claimed:
                continue
            score = self._score(nick_norm, norm)
            if field_name == "correct_nickname":
                score = min(100.0, score * self._CORRECT_BOOST)
            existing = per_record_best.get(idx)
            if existing is None or self._candidate_is_better(
                score, field_name, existing.score, existing.matched_field,
            ):
                per_record_best[idx] = MatchCandidate(
                    record_index=idx,
                    correct_nickname=self.records[idx].correct_nickname,
                    matched_field=field_name,
                    matched_value=raw,
                    score=score,
                )

        if not per_record_best:
            return MatchResult(
                captured_nickname=captured_nickname,
                decision=MatchDecision.NEW,
                best=None,
                alternatives=[],
            )

        ranked = sorted(
            per_record_best.values(),
            key=lambda c: (-c.score, -self._FIELD_TRUST[c.matched_field]),
        )
        best = ranked[0]
        alternatives = ranked[1:4]  # at most top 3 runners-up

        if best.score >= self.high_threshold:
            decision = MatchDecision.AUTO_HIGH
        elif best.score >= self.auto_threshold:
            decision = MatchDecision.AUTO_LOW
        elif best.score >= self.review_threshold:
            decision = MatchDecision.REVIEW
        else:
            decision = MatchDecision.NEW
            best = None

        return MatchResult(
            captured_nickname=captured_nickname,
            decision=decision,
            best=best,
            alternatives=alternatives,
        )

    # ------------------------------------------------------------- scoring

    @staticmethod
    def _score(a_norm: str, b_norm: str) -> float:
        """Run a handful of scorers and return the most generous one.

        ``ratio`` is the natural fit for short CJK strings (treats each
        character as a token). ``partial_ratio`` rescues cases where one
        side dropped a decorative glyph at either end (the in-game
        en-dashes that the detector misses). ``token_set_ratio`` cleans
        up name-with-spaces mismatches that creep in with mixed CJK + Latin.
        """
        if a_norm == b_norm:
            return 100.0
        scores = (
            fuzz.ratio(a_norm, b_norm),
            fuzz.partial_ratio(a_norm, b_norm),
            fuzz.token_set_ratio(a_norm, b_norm),
        )
        return max(scores)

    @classmethod
    def _candidate_is_better(
        cls, new_score: float, new_field: str,
        old_score: float, old_field: str,
    ) -> bool:
        if new_score != old_score:
            return new_score > old_score
        return cls._FIELD_TRUST[new_field] > cls._FIELD_TRUST[old_field]
