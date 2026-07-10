"""Multi-value Last_OCR_ID cells, shared by the gear workbook and the
league roster.

A Last_OCR_ID cell stores SEVERAL historical OCR variants per member —
weekly scans produce near-identical-but-not-equal spellings of the same
nickname, and keeping only the latest loses exact-match keys that were
still doing work (the league learned this first with five screens per
battle; gear scans hit the same problem week over week). Variants are
joined newest-first with a fullwidth bar — a character that can't appear
in nicknames. Cap 8 (2026-07-09, was 3): a single league battle can
legitimately contribute up to five spellings, so 3 could evict variants
that were still exact-matching the very next week.

Originally these helpers lived in ``league/roster.py``; they moved here
on 2026-07-10 when the gear workbook adopted the same multi-value format.
"""
from __future__ import annotations

from typing import Sequence, TYPE_CHECKING

from .matcher import Matcher, normalize_for_match

if TYPE_CHECKING:
    from ..storage.excel import PlayerRecord

OCR_SEP = "｜"
MAX_OCR_VARIANTS = 8


def split_ocr_ids(raw: object) -> list[str]:
    """Split a stored Last_OCR_ID cell into its variant list.

    Accepts any openpyxl cell value — a hand-typed numeric cell is
    str()ed rather than discarded."""
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(OCR_SEP) if part.strip()]


def primary_ocr_id(raw: object) -> str:
    """The newest stored variant — what UIs should show when they need a
    single OCR spelling to caption a row with (the full multi-value cell
    reads like line noise in a table)."""
    variants = split_ocr_ids(raw)
    return variants[0] if variants else ""


def merge_ocr_variant(existing_raw: object, new_name: str) -> str:
    """Prepend ``new_name`` to the stored variant list (deduped by the
    match normalisation, newest first, capped at MAX_OCR_VARIANTS)."""
    new_norm = normalize_for_match(new_name)
    variants = [new_name]
    for old in split_ocr_ids(existing_raw):
        if normalize_for_match(old) == new_norm:
            continue
        variants.append(old)
    return OCR_SEP.join(variants[:MAX_OCR_VARIANTS])


def build_matcher(records: Sequence["PlayerRecord"], **matcher_kwargs) -> Matcher:
    """Build a :class:`Matcher` over records whose ``latest_ocr_nickname``
    holds a multi-value Last_OCR_ID cell.

    Every stored variant is fed to the matcher as its own exact key, and
    the raw cell itself is NOT registered (``use_latest_ocr_field=False``)
    — the concatenated normalisation of 「A｜B｜C」 is a junk key that can
    only ever false-match. Always construct matchers through this factory
    so the two settings can't drift apart.
    """
    aliases = {
        i: variants
        for i, rec in enumerate(records)
        if (variants := split_ocr_ids(rec.latest_ocr_nickname))
    }
    return Matcher(
        records,
        ocr_aliases=aliases,
        use_latest_ocr_field=False,
        **matcher_kwargs,
    )
