"""Discover LDPlayer ADB endpoints.

LDPlayer 9 exposes ADB on 127.0.0.1 with the first instance at port 5555 and
each additional instance offset by +2 (5557, 5559, ...). Older instances may
also surface as ``emulator-5554`` etc. via ``adb devices`` without an explicit
``connect``.

Two probes live here:

  * :func:`find_ldplayer_devices` — pure ADB-side enumeration. Returns
    only what ADB itself can see. Fast, but invisible to the user when
    the instance is up but its ADB debug toggle is off.
  * :func:`find_ldplayer_instances` — preferred entry point for the GUI.
    Uses :mod:`ldconsole` to enumerate every multi-instance by name,
    then cross-references with ADB to mark which ones are actually
    reachable. This is what lets the GUI tell the user "instance #1 is
    running but its ADB偵錯 isn't enabled" instead of just "no devices".
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .client import AdbClient, AdbDevice, AdbError
from .ldconsole import LDPlayerInstance, LdConsoleError, list_instances

# LDPlayer 9 instance N is reachable at 127.0.0.1:(5555 + 2 * N)
# 16 candidates covers up to LDPlayer instance #15, which is plenty.
DEFAULT_PORTS: tuple[int, ...] = tuple(5555 + 2 * i for i in range(16))


@dataclass(frozen=True)
class DiscoveredDevice:
    serial: str
    state: str
    via_port: int | None  # None if discovered via `adb devices` (e.g. emulator-XXXX)

    @property
    def is_online(self) -> bool:
        return self.state == "device"


def find_ldplayer_devices(
    client: AdbClient | None = None,
    ports: tuple[int, ...] = DEFAULT_PORTS,
    host: str = "127.0.0.1",
) -> list[DiscoveredDevice]:
    """Probe candidate ADB ports and return the unique set of devices.

    The probe is conservative: we call ``adb connect`` for each port (cheap,
    returns immediately if nothing is listening) and then ``adb devices`` once
    to enumerate everything ADB now knows about.
    """
    client = client or AdbClient()

    # Make sure the daemon is up; otherwise every connect call would start it
    # in turn and add latency.
    try:
        client.start_server()
    except AdbError:
        pass

    for port in ports:
        try:
            client.connect(host, port)
        except AdbError:
            # Nothing listening on that port — perfectly normal.
            continue

    devices = _safe_devices(client)
    seen: dict[str, DiscoveredDevice] = {}
    for d in devices:
        port = _port_from_serial(d.serial)
        seen[d.serial] = DiscoveredDevice(d.serial, d.state, port)
    return sorted(seen.values(), key=lambda x: (x.via_port or 0, x.serial))


def _safe_devices(client: AdbClient) -> list[AdbDevice]:
    try:
        return client.devices()
    except AdbError:
        return []


def _port_from_serial(serial: str) -> int | None:
    if ":" not in serial:
        return None
    _, _, port = serial.rpartition(":")
    try:
        return int(port)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ldconsole-aware combined probe
# ---------------------------------------------------------------------------


class InstanceStatus(str, Enum):
    """High-level health of one LDPlayer multi-instance.

    Each value maps to a clear next action the user can take, so the
    GUI can render it as the dropdown sub-label.
    """

    # Process not running at all — user needs to start the instance.
    NOT_RUNNING = "not_running"
    # Process is running, but ADB port not listening — user needs to
    # toggle ADB偵錯 inside the instance settings and reboot it.
    ADB_OFF = "adb_off"
    # ADB connected and shell works. Ready to scan.
    ONLINE = "online"
    # ADB shows the device but it's offline / unauthorized. Rare on
    # LDPlayer (more common on real phones). Surface verbatim.
    OFFLINE = "offline"


@dataclass
class InstanceProbeResult:
    """Per-instance combined info from ldconsole + ADB."""

    instance: LDPlayerInstance
    status: InstanceStatus
    serial: str | None       # set when ADB sees the device, else None
    detail: str              # human-readable hint shown in the GUI

    @property
    def display_name(self) -> str:
        """The label shown in the GUI dropdown."""
        return f"[{self.instance.index}] {self.instance.name}"

    @property
    def is_usable(self) -> bool:
        return self.status == InstanceStatus.ONLINE


def find_ldplayer_instances(
    ldconsole_path: str | Path | None,
    client: AdbClient | None = None,
) -> list[InstanceProbeResult]:
    """Combine :func:`ldconsole.list_instances` with an ADB liveness check.

    The result tells the GUI **per instance** what's happening:

      * Process not running → ``NOT_RUNNING``.
      * Process running but no ADB port listening → ``ADB_OFF`` with a
        hint pointing the user to LDPlayer 設定 → 其他設定 → ADB 偵錯.
      * Process running, ADB port responds, shell echo round-trips → ``ONLINE``.
      * Process running, ADB sees device but state isn't ``device`` →
        ``OFFLINE`` (we surface the raw state in ``detail``).

    Returns an empty list if ldconsole isn't available or fails.
    """
    if ldconsole_path is None:
        return []
    try:
        instances = list_instances(ldconsole_path)
    except LdConsoleError:
        return []

    client = client or AdbClient()
    # Ensure the daemon is up so individual `connect` calls are cheap.
    try:
        client.start_server()
    except AdbError:
        pass

    # Pre-collect everything ADB already knows so we don't have to call
    # `adb devices` per instance.
    known_devices: dict[str, AdbDevice] = {}
    for d in _safe_devices(client):
        known_devices[d.serial] = d

    results: list[InstanceProbeResult] = []
    for inst in instances:
        if not inst.is_running:
            results.append(InstanceProbeResult(
                instance=inst,
                status=InstanceStatus.NOT_RUNNING,
                serial=None,
                detail="未啟動（請在 LDMultiplayer 點此實例開啟）",
            ))
            continue

        serial = inst.expected_serial
        # Try to bring it into adb's known list. Cheap if there's nothing
        # to connect to — adb returns almost immediately.
        try:
            client.connect("127.0.0.1", inst.adb_port)
        except AdbError:
            # Port not listening — that's the "ADB 偵錯沒開" case.
            results.append(InstanceProbeResult(
                instance=inst,
                status=InstanceStatus.ADB_OFF,
                serial=None,
                detail=(
                    f"ADB 偵錯未啟用 (port {inst.adb_port} not listen) — "
                    "請至其他設定 → ADB 偵錯 → 選擇開啟本地連接"
                ),
            ))
            continue

        # connect reported success — re-read the device list to confirm.
        refreshed = _safe_devices(client)
        match = next((d for d in refreshed if d.serial == serial), None)
        if match is None:
            results.append(InstanceProbeResult(
                instance=inst,
                status=InstanceStatus.ADB_OFF,
                serial=None,
                detail=(
                    f"ADB 連線異常 — connect 成功但 adb devices 看不到 {serial}"
                ),
            ))
            continue

        if match.state != "device":
            results.append(InstanceProbeResult(
                instance=inst,
                status=InstanceStatus.OFFLINE,
                serial=serial,
                detail=f"ADB 狀態 = {match.state!r}（合法值應為 'device'）",
            ))
            continue

        # Final smoke test — round-trip a shell command. Catches the
        # `host` pseudo-device case from stale adb server state.
        try:
            client.shell("true", serial=serial)
            results.append(InstanceProbeResult(
                instance=inst,
                status=InstanceStatus.ONLINE,
                serial=serial,
                detail=f"已連線 ({inst.width}×{inst.height})",
            ))
        except AdbError as exc:
            results.append(InstanceProbeResult(
                instance=inst,
                status=InstanceStatus.OFFLINE,
                serial=serial,
                detail=f"ADB 連上但 shell 失敗：{exc}",
            ))

    return results
