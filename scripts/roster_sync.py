"""CLI for the Google Sheet → local workbooks roster sync.

Fetches the guild roster sheet (first run opens the browser for Google
consent), diffs it against guild_scores.xlsx + league_scores.xlsx, then
walks through every change one by one for confirmation before writing.

Usage:
  .venv/Scripts/python.exe scripts/roster_sync.py             # interactive
  .venv/Scripts/python.exe scripts/roster_sync.py --dry-run   # diff only
  .venv/Scripts/python.exe scripts/roster_sync.py --forget-auth
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_gearsync.roster_sync.apply import ApplyError, apply_plan  # noqa: E402
from ro_gearsync.roster_sync.diff import (  # noqa: E402
    DECISION_APPLY,
    DECISION_RENAME,
    DECISION_REPLACE,
    DECISION_SKIP,
    KIND_CHANGED,
    KIND_LABEL,
    WorkbookLoadError,
    compute_plan,
)
from ro_gearsync.roster_sync.google_sheets import (  # noqa: E402
    SheetAccessError,
    clear_token,
    fetch_sheet_grid,
)
from ro_gearsync.roster_sync.sheet_parse import (  # noqa: E402
    SheetParseError,
    parse_roster_grid,
)


def _ask(prompt: str, choices: dict[str, str]) -> str:
    """Prompt until the user types one of ``choices`` keys."""
    menu = "／".join(f"[{k}]{v}" for k, v in choices.items())
    while True:
        ans = input(f"{prompt} {menu}: ").strip().lower()
        if ans in choices:
            return ans
        print(f"  請輸入 {'、'.join(choices)} 其中之一")


def main() -> int:
    parser = argparse.ArgumentParser(description="雲端名冊同步（CLI）")
    parser.add_argument("--url", help="覆寫 config.ini 的 sheet_url（測試用）")
    parser.add_argument("--guild", type=Path, help="覆寫裝評工作簿路徑")
    parser.add_argument("--league", type=Path, help="覆寫聯賽名冊路徑")
    parser.add_argument("--dry-run", action="store_true",
                        help="只顯示差異，不寫入")
    parser.add_argument("--forget-auth", action="store_true",
                        help="清除已存的 Google 授權後結束（下次同步重新授權）")
    args = parser.parse_args()

    if args.forget_auth:
        clear_token()
        print("已清除 Google 授權。")
        return 0

    try:
        grid = fetch_sheet_grid(args.url, status_cb=lambda m: print(f"  {m}"))
        parsed = parse_roster_grid(grid)
        plan = compute_plan(parsed, args.guild, args.league)
    except (SheetAccessError, SheetParseError, WorkbookLoadError) as exc:
        print(f"✗ {exc}")
        return 1

    print(f"\n試算表成員數（遊戲ID 非空白）：{plan.sheet_member_count}")
    for w in plan.warnings:
        print(f"⚠ {w}")

    if not plan.changes:
        print("✓ 兩份 Excel 與試算表一致，沒有需要同步的變更。")
        return 0

    print(f"\n共 {len(plan.changes)} 筆變更：")
    for c in plan.changes:
        print(f"  ID {c.member_id}｜{KIND_LABEL[c.kind]}｜{c.summary()}")
        for note in c.notes:
            print(f"      ⚠ {note}")

    if args.dry_run:
        print("\n（--dry-run：未寫入）")
        return 0

    print("\n逐筆確認：")
    decisions: dict[int, str] = {}
    for c in plan.changes:
        print(f"\nID {c.member_id}｜{KIND_LABEL[c.kind]}｜{c.summary()}")
        for note in c.notes:
            print(f"  ⚠ {note}")
        if c.kind == KIND_CHANGED:
            ans = _ask("  這是？", {
                "r": "換人（清空舊資料含裝評紀錄）",
                "m": "同一人改名/換職業（保留裝評紀錄）",
                "n": "略過",
            })
            decisions[c.member_id] = {
                "r": DECISION_REPLACE, "m": DECISION_RENAME, "n": DECISION_SKIP,
            }[ans]
        else:
            ans = _ask("  套用？", {"y": "套用", "n": "略過"})
            decisions[c.member_id] = (
                DECISION_APPLY if ans == "y" else DECISION_SKIP
            )

    if all(d == DECISION_SKIP for d in decisions.values()):
        print("\n全部略過，未寫入。")
        return 0

    try:
        report = apply_plan(plan, decisions)
    except ApplyError as exc:
        print(f"✗ {exc}")
        return 1

    print(f"\n✓ 已套用 {report.applied_count} 筆、略過 {report.skipped} 筆")
    for line in report.applied:
        print(f"  {line}")
    for b in report.backups:
        print(f"  備份：{b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
