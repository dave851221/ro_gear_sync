"""Background workers for the 聯賽評分 tab.

Mirrors :mod:`scan_runner`'s pattern: worker threads push ``(kind, *payload)``
tuples onto :attr:`LeagueRunner.events`; the Tk side polls the queue with
``after`` and repaints. No Tk calls happen on worker threads.

Two thread flavours:

  * **capture** — one at a time (owns ADB). Fast screenshot loop for one
    screen; the moment it finishes, the user may switch the game to the
    next screen and start the next capture while…
  * **recognition** — one thread per captured screen, all serialised
    through a single semaphore so concurrent screens can't exceed the
    Gemini free-tier requests-per-minute budget.

Event kinds:

  ("capture_progress", screen_key, n_pages)
  ("capture_done",     screen_key, n_pages, halt_reason)
  ("capture_error",    screen_key, exc)
  ("analyze_progress", screen_key, done, total, unique)
  ("analyze_retry",    screen_key, message)       # Gemini stalling, retrying
  ("analyze_done",     screen_key, scan)          # league.model.Scan
  ("analyze_error",    screen_key, exc)
"""
from __future__ import annotations

import queue
import threading

from ..adb import AdbClient
from ..league import LeagueCaptureSession, recognize_screen
from ..league.model import BATTLEFIELD_LABEL, VIEW_LABEL, Battlefield, Scan, View
from ..league.recognizer import Recognizer, create_recognizer
from ..utils.logging import logger

# The five league screens, in suggested scan order.
SCREEN_DEFS: list[tuple[Battlefield, View]] = [
    ("main", "dps"),
    ("main", "support"),
    ("sub", "dps"),
    ("sub", "support"),
    ("sub", "strategy"),
]


def screen_key(battlefield: Battlefield, view: View) -> str:
    return f"{battlefield}_{view}"


def screen_label(battlefield: Battlefield, view: View) -> str:
    return f"{BATTLEFIELD_LABEL[battlefield]}戰場・{VIEW_LABEL[view]}"


class LeagueRunner:
    """Owns the capture/recognition threads for one battle's worth of scans.

    Reusable across the whole app session — each 拍攝 click calls
    :meth:`start_capture`; results accumulate in :attr:`scans` until the
    user writes them out (then call :meth:`reset` for the next battle).
    """

    def __init__(self) -> None:
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.scans: dict[str, Scan] = {}
        # Captured pages kept per screen so a failed analysis (e.g. Gemini
        # 503 storm) can be retried WITHOUT recapturing.
        self.captured: dict[str, object] = {}
        self._gate = threading.Semaphore(1)
        self._recognizer: Recognizer | None = None
        self._recognizer_lock = threading.Lock()
        self._capture_thread: threading.Thread | None = None
        self._analyze_threads: list[threading.Thread] = []

    # ------------------------------------------------------------- state

    @property
    def capturing(self) -> bool:
        t = self._capture_thread
        return t is not None and t.is_alive()

    @property
    def analyzing(self) -> bool:
        return any(t.is_alive() for t in self._analyze_threads)

    def reset(self) -> None:
        """Forget accumulated scans (call after a successful write-out)."""
        self.scans = {}
        self.captured = {}

    def retry_analysis(self, key: str) -> bool:
        """Re-run recognition on already-captured pages (no recapture).

        Returns False when the screen was never captured this battle.
        """
        captured = self.captured.get(key)
        if captured is None:
            return False
        self._start_analysis(key, captured)
        return True

    def start_analysis_from_pages(
        self, battlefield: Battlefield, view: View, pages_dir,
    ) -> bool:
        """Analyse a previous capture session's saved pages (工具 →
        重新分析聯賽截圖). Returns False when the folder holds no PNGs."""
        from pathlib import Path

        from ..league.session import CapturedScreen

        pages_dir = Path(pages_dir)
        pages = sorted(pages_dir.glob("*.png"))
        if not pages:
            return False
        captured = CapturedScreen(
            battlefield=battlefield, view=view,
            session_dir=pages_dir.parent, pages=list(pages),
            halt_reason="reanalyze",
        )
        key = screen_key(battlefield, view)
        self.scans.pop(key, None)  # stale result would mask the re-run
        self._start_analysis(key, captured)
        return True

    # ------------------------------------------------------------ workers

    def _get_recognizer(self) -> Recognizer:
        """Lazy singleton, built from config ([league] backend → gemini/local).

        Raises RecognizerError when the gemini backend has no API key.
        """
        with self._recognizer_lock:
            if self._recognizer is None:
                self._recognizer = create_recognizer()
            return self._recognizer

    def start_capture(
        self,
        adb: AdbClient,
        battlefield: Battlefield,
        view: View,
    ) -> bool:
        """Kick off the capture thread for one screen.

        Returns False (and does nothing) when another capture is running —
        the panel disables the buttons, this is just a belt-and-braces guard.
        """
        if self.capturing:
            return False
        key = screen_key(battlefield, view)

        def _run() -> None:
            try:
                session = LeagueCaptureSession(adb, battlefield, view)
                captured = session.capture(
                    progress=lambda n: self.events.put(("capture_progress", key, n)),
                )
                self.events.put(
                    ("capture_done", key, len(captured.pages), captured.halt_reason)
                )
            except BaseException as exc:  # noqa: BLE001
                logger.exception("league capture failed for {}", key)
                self.events.put(("capture_error", key, exc))
                return
            self._start_analysis(key, captured)

        self._capture_thread = threading.Thread(
            target=_run, name=f"league-capture-{key}", daemon=True,
        )
        self._capture_thread.start()
        return True

    def _start_analysis(self, key: str, captured) -> None:
        self.captured[key] = captured
        # Prune finished workers so the list doesn't grow for the whole
        # app session (it's only consulted via ``analyzing`` anyway).
        self._analyze_threads = [t for t in self._analyze_threads if t.is_alive()]

        def _run() -> None:
            try:
                recognizer = self._get_recognizer()
                scan = recognize_screen(
                    recognizer,
                    captured,
                    progress=lambda d, t, u: self.events.put(
                        ("analyze_progress", key, d, t, u)
                    ),
                    retry_notice=lambda msg: self.events.put(
                        ("analyze_retry", key, msg)
                    ),
                    gate=self._gate,
                )
            except BaseException as exc:  # noqa: BLE001
                logger.exception("league analysis failed for {}", key)
                self.events.put(("analyze_error", key, exc))
                return
            self.scans[key] = scan
            self.events.put(("analyze_done", key, scan))

        t = threading.Thread(target=_run, name=f"league-analyze-{key}", daemon=True)
        t.start()
        self._analyze_threads.append(t)
