"""Launch the RO_GearSync customtkinter GUI.

Usage::

    .venv\\Scripts\\python.exe scripts\\run_gui.py

Equivalent to ``python -m ro_gearsync.gui.app`` but stays consistent with
the rest of the scripts/ entry points.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def _fix_tcl_paths() -> None:
    """Point Tcl/Tk at the real library directories on this Python install.

    Some Windows Python installs (notably the one at C:\\Software\\Python)
    ship Tcl/Tk under ``<prefix>/tcl/tcl8.6/`` while tkinter's default
    search expects ``<prefix>/lib/tcl8.6/``. Inside a venv ``sys.prefix``
    is the venv root (which has no Tcl files at all) and ``sys.base_prefix``
    is the system Python — that's where the libs actually live, so we
    point the env vars there.
    """
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    # Two layouts in the wild: <base>/tcl/tcl8.6 (most installers) and
    # <base>/lib/tcl8.6 (alt layout). Try both, accept the first hit.
    candidates_tcl = (base / "tcl" / "tcl8.6", base / "lib" / "tcl8.6")
    candidates_tk = (base / "tcl" / "tk8.6", base / "lib" / "tk8.6")
    for cand in candidates_tcl:
        if (cand / "init.tcl").is_file():
            os.environ.setdefault("TCL_LIBRARY", str(cand))
            break
    for cand in candidates_tk:
        if (cand / "tk.tcl").is_file():
            os.environ.setdefault("TK_LIBRARY", str(cand))
            break


_fix_tcl_paths()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ro_gearsync.gui.app import run  # noqa: E402
from ro_gearsync.utils.logging import setup as setup_logging  # noqa: E402


if __name__ == "__main__":
    setup_logging("INFO")
    raise SystemExit(run())
