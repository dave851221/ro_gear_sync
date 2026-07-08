"""Turn the raw sheet grid into ``{編號: SheetMember}``.

The guild sheet's layout (observed 2026-07-08):

  * a few banner rows, then a header row 編號/Line ID/遊戲ID/職業/…;
  * one row per member slot, 編號 ∈ 1..150 in **arbitrary order**,
    blank 遊戲ID = vacant slot;
  * below the member table sits a second table（排隊名單）whose 編號
    restarts at 1 — it must NOT be read as members.

Scan rules (per the 2026-07-08 spec):
  * header row = first row containing all of 編號/遊戲ID/職業;
  * collect rows whose 編號 parses to an int in 1..150, others skipped;
  * STOP once 150 distinct 編號 are collected, or when another
    header-looking row / the 排隊名單 banner shows up (protects against
    a member table with fewer than 150 rows bleeding into the queue);
  * duplicate 編號 before the stop → warning, that 編號 excluded entirely.
"""
from __future__ import annotations

from dataclasses import dataclass

HEADER_ID = "編號"
HEADER_NICK = "遊戲ID"
HEADER_PROF = "職業"
QUEUE_BANNER = "排隊名單"
MAX_MEMBER_ID = 150


class SheetParseError(RuntimeError):
    """Display-ready parse failure (e.g. header row not found)."""


@dataclass
class SheetMember:
    member_id: int
    nickname: str      # stripped; "" = vacant slot
    profession: str    # stripped; "" allowed


@dataclass
class ParsedSheet:
    members: dict[int, SheetMember]
    warnings: list[str]
    # 編號 that appeared more than once — excluded from ``members`` AND
    # from the diff entirely (their absence must not read as "left").
    excluded_ids: set[int]


def _strip(cell: str) -> str:
    return (cell or "").strip()


def _norm_header(cell: str) -> str:
    """Header cells compare whitespace-free — the live sheet writes
    「遊戲 ID」(with a space) where the local workbooks use「遊戲ID」."""
    return "".join((cell or "").split())


def _is_header_row(cells: list[str]) -> bool:
    normed = {_norm_header(c) for c in cells}
    return {HEADER_ID, HEADER_NICK, HEADER_PROF} <= normed


def parse_roster_grid(grid: list[list[str]]) -> ParsedSheet:
    """Duplicated 編號 are dropped from ``members``, reported in
    ``warnings`` and recorded in ``excluded_ids``."""
    header_idx = None
    for i, row in enumerate(grid):
        if _is_header_row(row):
            header_idx = i
            break
    if header_idx is None:
        raise SheetParseError(
            f"試算表裡找不到表頭列（需同時含「{HEADER_ID}」「{HEADER_NICK}」"
            f"「{HEADER_PROF}」三欄）。"
        )
    header = [_norm_header(c) for c in grid[header_idx]]
    col_id = header.index(HEADER_ID)
    col_nick = header.index(HEADER_NICK)
    col_prof = header.index(HEADER_PROF)

    members: dict[int, SheetMember] = {}
    duplicated: set[int] = set()
    warnings: list[str] = []
    seen: set[int] = set()

    for row in grid[header_idx + 1:]:
        cells = [_strip(c) for c in row]
        # Second table's header (排隊名單 etc) — the member table is over.
        if _is_header_row(cells) or any(QUEUE_BANNER in c for c in cells):
            break

        raw_id = cells[col_id] if col_id < len(cells) else ""
        try:
            mid = int(raw_id)
        except ValueError:
            continue  # blank spacer / stray text row
        if not 1 <= mid <= MAX_MEMBER_ID:
            continue

        nickname = cells[col_nick] if col_nick < len(cells) else ""
        profession = cells[col_prof] if col_prof < len(cells) else ""

        if mid in seen:
            if mid not in duplicated:
                duplicated.add(mid)
                warnings.append(
                    f"試算表上編號 {mid} 出現多次 — 此編號整筆不同步，"
                    "請先修正試算表。"
                )
            members.pop(mid, None)
        else:
            seen.add(mid)
            members[mid] = SheetMember(mid, nickname, profession)

        if len(seen) >= MAX_MEMBER_ID:
            break  # all member slots found; rows below are other data

    if not members:
        raise SheetParseError("表頭之後找不到任何編號 1–150 的成員列。")
    return ParsedSheet(members=members, warnings=warnings, excluded_ids=duplicated)
