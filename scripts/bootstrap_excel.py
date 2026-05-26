"""Bootstrap the guild scores workbook from a capture session.

Reads the latest (or specified) capture session's ``members.json`` and
writes ``data/guild_scores.xlsx`` — including a blank ``correct_nickname``
column for the user to fill out as ground truth.

Usage::

    .venv\\Scripts\\python.exe scripts\\bootstrap_excel.py
    .venv\\Scripts\\python.exe scripts\\bootstrap_excel.py --session 20260518_012234
    .venv\\Scripts\\python.exe scripts\\bootstrap_excel.py --date 2026-05-18
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_gearsync.storage import GuildScoresWorkbook  # noqa: E402
from ro_gearsync.utils.paths import user_data_dir  # noqa: E402


def pick_latest_session() -> Path | None:
    base = user_data_dir() / "captures"
    if not base.is_dir():
        return None
    sessions = sorted(
        [p for p in base.iterdir() if p.is_dir() and (p / "members.json").exists()]
    )
    return sessions[-1] if sessions else None


def session_label(session_dir: Path) -> str:
    """Derive a calendar-day column header from a session folder name.

    v2 collapses every capture on the same day into one column, so the
    label is just ``YYYY-MM-DD`` regardless of the HHMMSS suffix on the
    folder.
    """
    name = session_dir.name
    if (
        len(name) >= 8
        and name[:8].isdigit()
    ):
        return f"{name[0:4]}-{name[4:6]}-{name[6:8]}"
    return date.today().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--session",
        help="Session folder name under data/captures/ (defaults to latest).",
    )
    parser.add_argument(
        "--label",
        help="Date-column header (YYYY-MM-DD); overrides the value "
        "inferred from the session folder name.",
    )
    parser.add_argument(
        "--members-file",
        default="members.json",
        help="Which members*.json inside the session folder to consume. "
        "Use e.g. members_v5-server.json to bootstrap from a re-OCR'd variant.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output workbook path. Defaults to data/guild_scores.xlsx.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing workbook (will still take a backup).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Merge the new capture into the existing workbook instead of "
        "overwriting. Preserves user-edited correct_nickname / notes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the merge against an in-memory copy and print the three "
        "report sections, but never save back to the workbook.",
    )
    args = parser.parse_args()

    if args.session:
        session_dir = user_data_dir() / "captures" / args.session
    else:
        session_dir = pick_latest_session()
    if session_dir is None or not session_dir.is_dir():
        print("!! no capture session found under data/captures/")
        return 2

    members_path = session_dir / args.members_file
    if not members_path.is_file():
        print(f"!! {args.members_file} missing in {session_dir}")
        return 3
    print(f"[1] capture: {session_dir}")
    captured = json.loads(members_path.read_text(encoding="utf-8"))
    print(f"    {len(captured)} captured members")

    capture_label = args.label or session_label(session_dir)
    print(f"[2] capture date column header: {capture_label}")

    output = Path(args.output) if args.output else (user_data_dir() / "guild_scores.xlsx")
    if output.exists() and not args.force and not args.update:
        print(
            f"!! {output} already exists. Re-run with:\n"
            f"   --update  to merge the new capture into the existing file "
            f"(preserves correct_nickname)\n"
            f"   --force   to overwrite from scratch (still backs up)"
        )
        return 4
    print(f"[3] target workbook: {output}")

    if args.update and output.is_file():
        print(f"    mode: UPDATE (preserving user edits)")
        book = GuildScoresWorkbook.load(output)
        book.path = output  # ensure save target matches
        before_filled = sum(1 for r in book.records if r.correct_nickname)
        before_total = len(book.records)
        print(f"    loaded {before_total} existing rows "
              f"({before_filled} with correct_nickname filled)")
        result = book.merge_capture(captured, capture_label)
        print()
        print(f"=== Merge summary ===")
        print(f"  phase 1 exact correct_nickname  : {result.n_exact_correct}")
        print(f"  phase 2 exact latest_ocr_nick   : {result.n_exact_ocr}")
        print(f"  phase 3 fuzzy correct_nickname  : {result.n_fuzzy_correct}")
        print(f"  phase 4 fuzzy latest_ocr_nick   : {result.n_fuzzy_ocr}")
        print(f"  appended as NEW (red row)       : {result.n_new}")
        print(f"  missed in capture               : {len(result.missed_in_capture)}")
        print(f"  total workbook rows now         : {len(book.records)}")

        review_matches = [
            m for m in result.updated
            if m.match_via in ("fuzzy_correct", "fuzzy_ocr")
            and m.score is not None and m.score < 80.0
        ]
        if review_matches:
            print()
            print("[A] REVIEW — fuzzy 65-79, please eyeball:")
            for m in review_matches:
                score = f"{m.score:.0f}" if m.score is not None else "-"
                alt = f"  alts: {m.alternatives}" if m.alternatives else ""
                print(
                    f"    gear={m.gear:>7}  ocr={m.ocr_nickname!r:<20} "
                    f"-> {m.matched_to!r:<16} @{score} ({m.match_via}){alt}"
                )

        if result.unmatched_captured:
            print()
            print(
                f"[B] APPENDED NEW rows (red, fill correct_nickname): "
                f"{len(result.unmatched_captured)}"
            )
            for m in result.unmatched_captured:
                print(f"    gear={m.gear:>7}  ocr={m.ocr_nickname!r}")

        if result.missed_in_capture:
            print()
            print(
                f"[C] MISSED in capture — Excel rows NOT seen this round "
                f"({len(result.missed_in_capture)} rows):"
            )
            for m in result.missed_in_capture[:25]:
                print(f"    {m.ocr_nickname!r}  (peak gear {m.gear})")
            if len(result.missed_in_capture) > 25:
                print(
                    f"    ... and {len(result.missed_in_capture) - 25} more"
                )
    else:
        print(f"    mode: BOOTSTRAP (fresh workbook)")
        book = GuildScoresWorkbook(output)
        book.bootstrap_from_captures(captured, capture_label)
        print(f"    {len(book.records)} player rows prepared")

    review_counts: dict[str, int] = {}
    for rec in book.records:
        if rec.review_reason:
            review_counts[rec.review_reason] = review_counts.get(rec.review_reason, 0) + 1
    if review_counts:
        print(f"    review_reason breakdown:")
        for reason, n in review_counts.items():
            print(f"      {reason}: {n}")
    else:
        print("    no rows flagged for review")

    if args.dry_run:
        print()
        print("[!] --dry-run: nothing was written to disk.")
        return 0

    backup = book.save(backup=True)
    if backup:
        print(f"[4] previous workbook backed up to {backup}")
    print(f"[5] wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
