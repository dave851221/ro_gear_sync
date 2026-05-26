"""M3c probe — end-to-end guild capture with auto-scroll and dedup.

Connects to LDPlayer, runs the full capture loop, and writes a session
folder under ``data/captures/<timestamp>/`` containing every page image,
``members.json`` (dedup'd list), and ``summary.json``.

Before running:
  1. Open RO mobile in LDPlayer.
  2. Navigate to 公會 -> 公會成員 (guild member list).
  3. Scroll to the top of the list (or trust the script's prime swipes).

Usage::

    .venv\\Scripts\\python.exe scripts\\probe_capture.py [--max-pages N] [--serial X]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_gearsync.adb import AdbClient, AdbError, find_ldplayer_devices  # noqa: E402
from ro_gearsync.capture import CaptureSession  # noqa: E402
from ro_gearsync.utils.logging import logger, setup as setup_logging  # noqa: E402
from ro_gearsync.utils.paths import adb_binary  # noqa: E402
from ro_gearsync.vision import OcrEngine  # noqa: E402


def _pick_serial(client: AdbClient) -> str | None:
    """Prefer a TCP-style serial (e.g. 127.0.0.1:5555) over emulator-NNNN.

    Both refer to the same device, but the TCP one is easier to surface to
    the user and matches our port-scanner expectations.
    """
    found = find_ldplayer_devices(client)
    online = [d for d in found if d.is_online]
    if not online:
        return None
    tcp = [d for d in online if d.via_port is not None]
    return (tcp[0] if tcp else online[0]).serial


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default=None)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument(
        "--ocr-quality", default="v5-mobile",
        choices=["v4-mobile", "v4-server", "v5-mobile", "v5-server"],
        help="Primary OCR model (fast pass).",
    )
    parser.add_argument(
        "--fallback-quality", default="v5-server",
        choices=["", "v4-server", "v5-mobile", "v5-server"],
        help="Heavier model used to re-OCR low-confidence rows. "
        "Empty string disables fallback.",
    )
    parser.add_argument(
        "--fallback-threshold", type=float, default=0.80,
        help="Confidence below which a primary OCR result triggers fallback.",
    )
    parser.add_argument(
        "--no-prime", action="store_true",
        help="Skip the initial up-swipes that try to scroll the list to the top.",
    )
    args = parser.parse_args()

    setup_logging("INFO")

    located = adb_binary()
    print(f"[1] adb binary: {located or '(PATH)'}")
    client = AdbClient(binary=located, default_serial=args.serial)
    try:
        print(f"    adb version: {client.version()}")
    except AdbError as exc:
        print(f"!! adb invocation failed: {exc}")
        return 2

    serial = args.serial or _pick_serial(client)
    if not serial:
        print("!! no online LDPlayer device found.")
        return 3
    client.default_serial = serial
    w, h = client.screen_size()
    print(f"[2] device: {serial}    screen (native): {w} x {h}")

    print(f"[3] initialising primary OCR engine ({args.ocr_quality}) ...")
    ocr = OcrEngine(model_quality=args.ocr_quality)
    fallback_ocr: OcrEngine | None = None
    if args.fallback_quality:
        print(f"    initialising fallback OCR engine ({args.fallback_quality}) ...")
        fallback_ocr = OcrEngine(model_quality=args.fallback_quality)

    print("[4] starting capture loop (pipelined) ...")
    session = CaptureSession(
        adb=client,
        ocr=ocr,
        fallback_ocr=fallback_ocr,
        fallback_threshold=args.fallback_threshold,
        max_pages=args.max_pages,
        prime_with_up_swipes=0 if args.no_prime else 2,
    )

    def _progress(page: int, total: int, new: int) -> None:
        print(f"    page #{page:>2}: +{new} new  (total unique: {total})")

    try:
        result = session.run(progress=_progress)
    finally:
        # Drop the ADB bridge once we're done. The game's anti-cheat is
        # less twitchy when no debug bridge is active at launch time, and
        # we have no further use for it until the next scan.
        try:
            client.disconnect()
            print("[*] adb disconnected")
        except Exception as exc:
            print(f"[*] adb disconnect skipped: {exc}")

    print()
    print(f"[5] session dir: {result.session_dir}")
    print(f"    pages       : {len(result.pages)}")
    print(f"    unique      : {len(result.members)}")
    print(f"    halt reason : {result.halt_reason}")
    print(f"    duration    : {result.duration_seconds:.1f}s")

    print()
    print("[6] members (first 30):")
    print("-" * 72)
    print(f"{'idx':>3}  {'gear':>7}  {'sights':>6}  nickname")
    print("-" * 72)
    for i, m in enumerate(result.members[:30]):
        nick = (m.nickname or "—")
        print(
            f"{i:>3}  {m.gear_score:>7}  {m.sightings:>6}  "
            f"{nick}  ({m.nickname_confidence:.2f})"
            if m.nickname_confidence is not None
            else f"{i:>3}  {m.gear_score:>7}  {m.sightings:>6}  {nick}"
        )
    print("-" * 72)
    if len(result.members) > 30:
        print(f"    ... {len(result.members) - 30} more in members.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
