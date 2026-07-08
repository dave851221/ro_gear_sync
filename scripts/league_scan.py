"""Interactive CLI to capture one full league battle end-to-end (live).

Drives the real pipeline so we can validate the swipe-left-only scroll,
Gemini recognition, dedup, roster matching and Excel output on a device
*before* the GUI exists.

Flow: for each of the five screens (主×輸出/輔助＋副×輸出/輔助/戰略) you
switch the game to that screen and press Enter; the tool scrolls the LEFT
list, recognises each page via Gemini, and dedups. After all five (or the
ones you do), it matches to the league roster and writes
league_scores_YYYYMMDD_HHMM.xlsx.

Usage:
  .venv/Scripts/python.exe scripts/league_scan.py
  .venv/Scripts/python.exe scripts/league_scan.py --roster "G:/.../league_scores.xlsx" --model gemini-3.1-flash-lite
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

# Windows consoles often default to cp950, which can't render the status
# icons — force UTF-8 with replacement so printing never crashes the scan.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ro_gearsync.adb import AdbClient  # noqa: E402
from ro_gearsync.adb.port_scanner import (  # noqa: E402
    InstanceProbeResult,
    InstanceStatus,
    find_ldplayer_instances,
)
from ro_gearsync.league import (  # noqa: E402
    LeagueCaptureSession,
    create_recognizer,
    load_roster,
    merge_battle,
    recognize_screen,
    roster_issues,
    update_roster_ocr,
    write_battle,
)
from ro_gearsync.league.model import BATTLEFIELD_LABEL, VIEW_LABEL  # noqa: E402
from ro_gearsync.utils.paths import adb_binary, ldconsole_binary  # noqa: E402

# The five league screens (戰略 exists only on the sub battlefield).
SCREENS = [
    ("main", "dps"),
    ("main", "support"),
    ("sub", "dps"),
    ("sub", "support"),
    ("sub", "strategy"),
]

_STATUS_LABEL = {
    InstanceStatus.ONLINE: "✅ 已連線",
    InstanceStatus.ADB_OFF: "⚠ ADB 尚未啟用",
    InstanceStatus.NOT_RUNNING: "❌ 未啟動",
    InstanceStatus.OFFLINE: "⚠ 離線",
}


def pick_device() -> tuple[AdbClient, str]:
    """Mirror the GUI's env-probe flow: ldconsole enumerates instances,
    each running one gets `adb connect 127.0.0.1:(5555+2N)` + a shell smoke
    test; default to the first ONLINE instance (ask when several)."""
    adb_path = adb_binary()
    if adb_path is None:
        sys.exit("找不到 adb.exe（LDPlayer 安裝目錄）。")
    client = AdbClient(binary=adb_path)

    console_path = ldconsole_binary()
    if console_path is None:
        sys.exit("找不到 ldconsole.exe（LDPlayer 安裝目錄）。")

    instances = find_ldplayer_instances(console_path, client)
    if not instances:
        sys.exit("未列舉到任何 LDPlayer 多開器。請確認 LDPlayer 已開啟。")

    print("偵測到的多開器：")
    for r in instances:
        print(f"  [{r.instance.index}] {r.instance.name}  "
              f"{_STATUS_LABEL.get(r.status, r.status.value)} — {r.detail}")

    online = [r for r in instances if r.status == InstanceStatus.ONLINE and r.serial]
    if not online:
        sys.exit(
            "沒有可用（已連線）的實例。若實例正在執行，請至 LDPlayer "
            "設定 → 其他設定 → ADB 偵錯 → 開啟本地連接。"
        )
    if len(online) == 1:
        chosen = online[0]
    else:
        idx_map = {str(r.instance.index): r for r in online}
        ans = input(
            f"有多台已連線，請輸入編號（{'/'.join(idx_map)}，Enter＝第一台）："
        ).strip()
        chosen = idx_map.get(ans, online[0])
    print(f"使用：[{chosen.instance.index}] {chosen.instance.name} → {chosen.serial}")
    return client, chosen.serial  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", help="聯賽名冊 league_scores.xlsx 路徑（留空＝config 或 data/league_scores.xlsx）")
    ap.add_argument("--model", help="Gemini 模型名稱（留空＝config [gemini] model）")
    ap.add_argument("--out", help="聯賽檔輸出資料夾（留空＝名冊同目錄）")
    args = ap.parse_args()

    roster_path = Path(args.roster) if args.roster else None
    roster = load_roster(roster_path)
    n_real = sum(1 for r in roster if r.correct_nickname.strip())
    print(f"已讀取聯賽名冊：{n_real} 名成員（共 {len(roster)} 列，空位保留）")
    if not n_real:
        sys.exit("名冊是空的——請確認 league_scores.xlsx 路徑與內容。")
    issues = roster_issues(roster)
    if issues:
        print("⚠ 名冊有問題，建議先修正（受影響的成員無法回寫 Last_OCR_ID）：")
        for s in issues[:10]:
            print(f"  ・{s}")
        if len(issues) > 10:
            print(f"  …等共 {len(issues)} 項")

    client, serial = pick_device()
    client.default_serial = serial

    recognizer = create_recognizer(model=args.model)
    print(f"辨識引擎：Gemini（{recognizer.model}）\n")

    # Capture is fast and interactive; recognition runs on background
    # threads (one per captured screen), serialised through a single
    # semaphore so concurrent screens never exceed the free-tier RPM.
    gemini_gate = threading.Semaphore(1)
    scans: list = []
    scans_lock = threading.Lock()
    workers: list[threading.Thread] = []
    status: dict[str, str] = {}   # label → progress line

    def _start_recognition(captured, label: str) -> None:
        def _run() -> None:
            def _prog(done: int, total: int, unique: int) -> None:
                status[label] = f"{done}/{total} 頁，已辨識 {unique} 人"
            scan = recognize_screen(
                recognizer, captured, progress=_prog, gate=gemini_gate,
            )
            with scans_lock:
                scans.append(scan)
            status[label] = (f"✔ 完成：{len(scan.rows)} 人，"
                             f"參戰人數={scan.participant_count}")
        t = threading.Thread(target=_run, name=f"league-recognize-{label}",
                             daemon=True)
        t.start()
        workers.append(t)

    for bf, view in SCREENS:
        label = f"{BATTLEFIELD_LABEL[bf]}戰場/{VIEW_LABEL[view]}"
        ans = input(f"➤ 請在遊戲切到「{label}」畫面，按 Enter 開始拍攝"
                    f"（輸入 s 跳過這個畫面）：").strip().lower()
        if ans == "s":
            print(f"  已跳過 {label}\n")
            continue
        session = LeagueCaptureSession(client, bf, view)
        captured = session.capture(
            progress=lambda n: print(f"  📸 已拍 {n} 頁…", end="\r"),
        )
        print(f"\n  ✅ {label} 拍攝完成（{len(captured.pages)} 頁，"
              f"{captured.halt_reason}）— 可以切換下一個畫面了！（背景分析中）\n")
        status[label] = "排隊分析中…"
        _start_recognition(captured, label)

    if not workers:
        sys.exit("沒有拍攝任何畫面，結束。")

    # Wait for background recognition, showing live progress.
    print("等待背景分析完成…（每 5 秒更新進度）")
    while any(t.is_alive() for t in workers):
        for label, line in status.items():
            print(f"  {label}: {line}")
        print("  ---")
        time.sleep(5)
    for label, line in status.items():
        print(f"  {label}: {line}")

    if not scans:
        sys.exit("背景分析沒有產出任何結果，結束。")

    result = merge_battle(scans, roster)
    out_dir = Path(args.out) if args.out else None
    path = write_battle(result, out_dir)

    print("\n=== 對帳摘要 ===")
    for r in result.reconciliations:
        delta = "" if r.delta is None else f"（差 {r.delta:+d}）"
        # Reconciliation is per SCREEN — include the view or the two
        # 主戰場 lines would be indistinguishable.
        print(f"  {r.label}："
              f"參戰人數={r.participant_count} 辨識={r.recognized} "
              f"自動對應={r.auto_matched} 待review={r.review} {delta}")
    print(f"  總參與成員：{len(result.participants)} 人；"
          f"需人工確認：{len(result.review_players)} 列")
    if result.review_players:
        print("  待確認名單：")
        for p in result.review_players:
            print(f"    - {p.nickname}  ({p.review_note})")
    print(f"\n已寫入：{path}")

    # Self-healing loop: remember this battle's OCR strings so next scan
    # exact-matches them (fuzzy/unmatched rows are NOT written until a
    # human confirms — mirrors the gear workbook contract).
    n_updated = update_roster_ocr(result, roster_path)
    print(f"名冊回寫：更新 {n_updated} 位成員的 Last_OCR_ID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
