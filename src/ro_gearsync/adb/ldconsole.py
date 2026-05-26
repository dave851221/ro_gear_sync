"""LDPlayer 9 ``ldconsole.exe`` wrapper.

ldconsole is LDPlayer's CLI control tool. It can enumerate every
multi-instance the user has created (running or not) along with the name
they gave it in LDMultiplayer. That gives us a far better diagnostic
than blind ADB port scanning:

  * The GUI can list **every** instance, not just the ones whose ADB
    bridge happens to be up.
  * When an instance is running but no ADB port is listening, we can
    tell the user "this instance is up but ADB debug isn't enabled" —
    actionable, instead of just "未偵測到".
  * Instance index drives the predictable ADB port: ``5555 + 2 * N``.

``ldconsole list2`` output format (comma-separated, one row per instance)::

    index, name, top_handle, bind_handle, is_running, pid, vbox_pid,
    width, height, dpi
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Hide the console window when running from a PyInstaller --noconsole build.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class LdConsoleError(RuntimeError):
    """Raised when ldconsole invocation fails or output can't be parsed."""


@dataclass(frozen=True)
class LDPlayerInstance:
    """One LDPlayer multi-instance as reported by ``ldconsole list2``."""

    index: int
    name: str
    is_running: bool
    pid: int | None
    width: int
    height: int
    dpi: int

    @property
    def adb_port(self) -> int:
        """The TCP port this instance's ADB bridge listens on when enabled.

        LDPlayer 9 maps instance N to ``5555 + 2 * N``. Instance #0 is
        always 5555, regardless of any other configuration. Note that the
        port is only actually listening when ADB debug is toggled ON in
        the instance's own settings.
        """
        return 5555 + 2 * self.index

    @property
    def expected_serial(self) -> str:
        return f"127.0.0.1:{self.adb_port}"


def _run_ldconsole(
    binary: str | Path, args: Sequence[str], timeout: float = 10.0
) -> str:
    cmd = [str(binary), *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise LdConsoleError(
            f"could not find ldconsole at {binary!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LdConsoleError(f"ldconsole timed out: {' '.join(cmd)}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise LdConsoleError(
            f"ldconsole returned {result.returncode}: {' '.join(cmd)}\n{stderr}"
        )
    # LDPlayer outputs in the system code page on Windows. UTF-8 catches
    # most ASCII content; fall back to CP950 (Traditional Chinese default)
    # which is what LDPlayer ships with on the user's machine.
    raw = result.stdout
    for encoding in ("utf-8", "cp950", "mbcs"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def list_instances(binary: str | Path) -> list[LDPlayerInstance]:
    """Run ``ldconsole list2`` and return one record per instance.

    Returned list is sorted by ``index`` ascending so the GUI can display
    them in the same order LDMultiplayer does.
    """
    text = _run_ldconsole(binary, ["list2"])
    instances: list[LDPlayerInstance] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 10:
            # Older LDPlayer builds emit 8 fields; skip oddball lines
            # gracefully rather than crashing the whole probe.
            continue
        try:
            index = int(parts[0])
            name = parts[1]
            is_running = parts[4] == "1"
            pid = int(parts[5]) if parts[5] not in ("", "0") else None
            width = int(parts[7])
            height = int(parts[8])
            dpi = int(parts[9])
        except (ValueError, IndexError):
            continue
        instances.append(
            LDPlayerInstance(
                index=index,
                name=name,
                is_running=is_running,
                pid=pid,
                width=width,
                height=height,
                dpi=dpi,
            )
        )
    instances.sort(key=lambda i: i.index)
    return instances
