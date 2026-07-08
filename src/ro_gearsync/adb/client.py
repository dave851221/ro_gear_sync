"""Thin, typed wrapper around the ``adb`` command-line tool.

The whole capture pipeline talks to the LDPlayer emulator through ADB, which
gives us four important properties:

  * screencap returns the device-internal framebuffer, **independent of the
    host window size or position** — the emulator can be minimized, resized,
    or off-screen and we still get a clean PNG.
  * `input swipe` / `input tap` use device coordinates, so calibration is
    one-shot per resolution rather than per host window.
  * No focus stealing — the user can keep working on the host machine while
    a capture is running.
  * Works while the LDPlayer window is hidden behind other windows.

The class does not maintain a persistent ADB session; each call spawns a
short-lived subprocess. That avoids subtle hangs we have seen with long-lived
adb shell pipes on Windows, at the cost of a few extra ms per call.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..utils.paths import adb_binary

# Hide the console window when running from a PyInstaller --noconsole build.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class AdbError(RuntimeError):
    """Raised when an adb invocation fails or returns an unexpected result."""


@dataclass(frozen=True)
class AdbDevice:
    serial: str          # e.g. "127.0.0.1:5555" or "emulator-5554"
    state: str           # "device", "offline", "unauthorized", ...

    @property
    def is_online(self) -> bool:
        return self.state == "device"


class AdbClient:
    """Wraps a single ``adb`` binary and (optionally) a default device serial."""

    def __init__(
        self,
        binary: str | Path | None = None,
        default_serial: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        if binary is None:
            located = adb_binary()
            binary = str(located) if located else "adb"
        self.binary = str(binary)
        self.default_serial = default_serial
        self.timeout = timeout

    # ------------------------------------------------------------------ core

    def _run(
        self,
        args: Sequence[str],
        *,
        capture_binary: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = [self.binary, *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout or self.timeout,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise AdbError(
                f"Could not find adb binary at {self.binary!r}. "
                "Install Android Platform Tools or bundle adb.exe under bin/."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb command timed out: {' '.join(cmd)}") from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise AdbError(
                f"adb returned {result.returncode}: {' '.join(cmd)}\n{stderr}"
            )
        if not capture_binary:
            # Re-attach decoded stdout for convenience.
            result.stdout_text = result.stdout.decode(  # type: ignore[attr-defined]
                "utf-8", "replace"
            )
        return result

    def _device_args(self, serial: str | None) -> list[str]:
        s = serial or self.default_serial
        return ["-s", s] if s else []

    # -------------------------------------------------------------- top level

    def version(self) -> str:
        result = self._run(["version"])
        return result.stdout_text.splitlines()[0]  # type: ignore[attr-defined]

    def start_server(self) -> None:
        self._run(["start-server"])

    def kill_server(self) -> None:
        self._run(["kill-server"])

    def connect(self, host: str, port: int) -> str:
        """Run ``adb connect host:port`` and return the trimmed output."""
        result = self._run(
            ["connect", f"{host}:{port}"], timeout=min(self.timeout, 5.0)
        )
        text = result.stdout_text.strip()  # type: ignore[attr-defined]
        # adb returns 0 even for failures like "cannot connect"; inspect text.
        lowered = text.lower()
        if "connected" not in lowered and "already" not in lowered:
            raise AdbError(f"adb connect failed: {text}")
        return text

    def disconnect(self, host: str | None = None, port: int | None = None) -> str:
        target = []
        if host is not None and port is not None:
            target = [f"{host}:{port}"]
        result = self._run(["disconnect", *target])
        return result.stdout_text.strip()  # type: ignore[attr-defined]

    def devices(self) -> list[AdbDevice]:
        result = self._run(["devices"])
        out: list[AdbDevice] = []
        for line in result.stdout_text.splitlines()[1:]:  # type: ignore[attr-defined]
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                out.append(AdbDevice(serial=parts[0], state=parts[1]))
        return out

    # ------------------------------------------------------------ device ops

    def shell(self, command: str, *, serial: str | None = None) -> str:
        result = self._run([*self._device_args(serial), "shell", command])
        return result.stdout_text  # type: ignore[attr-defined]

    def exec_out(
        self, command: str, *, serial: str | None = None, timeout: float | None = None
    ) -> bytes:
        """Run ``adb exec-out`` and return raw stdout bytes.

        Prefer this over ``shell`` when transferring binary payloads
        (screencap PNG) — ``shell`` will mangle CR/LF on some adb builds and
        corrupt the PNG.
        """
        result = self._run(
            [*self._device_args(serial), "exec-out", command],
            capture_binary=True,
            timeout=timeout,
        )
        return result.stdout

    def screencap_png(
        self, *, serial: str | None = None, timeout: float | None = None
    ) -> bytes:
        """Grab a PNG screenshot of the device framebuffer."""
        data = self.exec_out("screencap -p", serial=serial, timeout=timeout or 10.0)
        if not data.startswith(b"\x89PNG"):
            # Fall back to "shell screencap -p". The stdout MUST stay raw
            # bytes here — ``shell()`` decodes to text, and a UTF-8
            # decode-with-replace of binary PNG data is irreversibly
            # lossy — so we invoke ``_run`` directly with
            # ``capture_binary=True`` and undo the CR/LF mangling some
            # adb builds apply to shell output.
            result = self._run(
                [*self._device_args(serial), "shell", "screencap -p"],
                capture_binary=True,
                timeout=timeout or 10.0,
            )
            data = result.stdout.replace(b"\r\n", b"\n")
            if not data.startswith(b"\x89PNG"):
                raise AdbError("screencap output is not a valid PNG")
        return data

    def screen_size(self, *, serial: str | None = None) -> tuple[int, int]:
        """Return (width, height) of the device screen in device pixels."""
        text = self.shell("wm size", serial=serial)
        # "Physical size: 1280x720"  or  "Override size: 720x1280"
        for line in text.splitlines():
            if ":" in line and "x" in line:
                value = line.split(":", 1)[1].strip()
                w, _, h = value.partition("x")
                try:
                    return int(w), int(h)
                except ValueError:
                    continue
        raise AdbError(f"could not parse wm size output: {text!r}")

    def tap(self, x: int, y: int, *, serial: str | None = None) -> None:
        self.shell(f"input tap {x} {y}", serial=serial)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 600,
        *,
        serial: str | None = None,
    ) -> None:
        self.shell(
            f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", serial=serial
        )

    def keyevent(self, code: int | str, *, serial: str | None = None) -> None:
        self.shell(f"input keyevent {code}", serial=serial)
