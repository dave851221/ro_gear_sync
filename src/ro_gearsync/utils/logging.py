"""Project-wide logger setup.

We use loguru and write rotating files into the runtime ``logs/`` folder while
mirroring to stderr at INFO level.
"""
from __future__ import annotations

import sys

from loguru import logger

from .paths import logs_dir

_CONFIGURED = False


def setup(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger.remove()
    # In a frozen --windowed PyInstaller build there is no console
    # attached, so ``sys.stderr`` is ``None`` — passing that to
    # ``logger.add`` raises ``TypeError: write() argument must be str``
    # before the GUI ever gets a chance to start. Skip the stderr
    # sink in that case; the file sink below still captures everything.
    if sys.stderr is not None:
        logger.add(sys.stderr, level=level, enqueue=False)
    logger.add(
        logs_dir() / "ro_gearsync_{time:YYYYMMDD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention=10,
        encoding="utf-8",
        enqueue=True,
    )
    _CONFIGURED = True


__all__ = ["logger", "setup"]
