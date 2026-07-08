"""Cross-context path helpers.

Three layers of path resolution, in priority order:

  1. **User config** (``config.ini`` next to the .exe; see :mod:`.config`).
     This is the user-editable override layer — anything they set there
     wins. Used so an installer can drop a custom ``ldplayer_dir`` or so
     the user can keep the Excel on a Google Drive synced folder.

  2. **Auto-detect**. For LDPlayer we walk a small list of well-known
     install locations (most LDPlayer installs land in one of them).

  3. **Bundled fallback**. PyInstaller ``--onefile`` builds carry their
     assets in ``sys._MEIPASS``; everything else is resolved relative to
     the project root in dev mode.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def bundle_dir() -> Path:
    """Directory where bundled assets (models, templates, adb.exe) reside."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return project_root()


def project_root() -> Path:
    """Repository root when running from source."""
    return Path(__file__).resolve().parents[3]


def _app_root() -> Path:
    """Folder the user thinks of as "where RO_GearSync lives".

    Frozen → next to the .exe. Dev → project root. Used as the anchor
    for default data/log paths and for the on-disk ``config.ini``.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def config_path() -> Path:
    """Where ``config.ini`` lives — next to the .exe (or project root)."""
    return _app_root() / "config.ini"


# --------------------------------------------------------------------- data

def user_data_dir() -> Path:
    """Where the user's Excel, captures, and backups live.

    Priority:
      1. ``[paths] data_dir`` from config.ini if set.
      2. ``<app root>/data`` (legacy default).
    """
    from .config import app_config
    cfg = app_config()
    target = cfg.data_dir if cfg.data_dir else (_app_root() / "data")
    target.mkdir(parents=True, exist_ok=True)
    return target


def logs_dir() -> Path:
    """Where loguru's rotating log files go."""
    from .config import app_config
    cfg = app_config()
    target = cfg.logs_dir if cfg.logs_dir else (_app_root() / "logs")
    target.mkdir(parents=True, exist_ok=True)
    return target


def backups_dir() -> Path:
    target = user_data_dir() / "backups"
    target.mkdir(parents=True, exist_ok=True)
    return target


def default_workbook_path() -> Path:
    """Configured Excel file location, falling back to data/guild_scores.xlsx."""
    from .config import app_config
    cfg = app_config()
    if cfg.workbook_path:
        return cfg.workbook_path
    return user_data_dir() / "guild_scores.xlsx"


def league_roster_path() -> Path:
    """The league member roster (``league_scores.xlsx``) — maintained by the
    user, fully separate from the gear workbook. Config ``[league]
    roster_path`` wins; default is ``<data>/league_scores.xlsx``."""
    from .config import app_config
    cfg = app_config()
    if cfg.league_roster_path:
        return cfg.league_roster_path
    return user_data_dir() / "league_scores.xlsx"


def google_token_path() -> Path:
    """OAuth token cache for the roster-sync Google Sheets access.

    Lives in the data dir (already gitignored / per-user) so the user
    only has to click through the browser consent once per machine."""
    return user_data_dir() / "google_token.json"


def league_output_dir() -> Path:
    """Folder where per-battle league snapshot files (``league_scores_*.xlsx``)
    are written. Config ``[league] output_dir`` wins; otherwise they sit
    next to the roster so all league files live together."""
    from .config import app_config
    cfg = app_config()
    target = cfg.league_output_dir or league_roster_path().parent
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------- LDPlayer

# Built-in auto-detect list, used when the user hasn't set ``ldplayer_dir``
# in config.ini. Sorted roughly by frequency so the early candidates hit
# fastest on typical installs.
_KNOWN_LDPLAYER_DIRS: tuple[Path, ...] = (
    Path(r"C:\LDPlayer\LDPlayer9"),
    Path(r"C:\LDPlayer\LDPlayer64"),
    Path(r"C:\Program Files\LDPlayer\LDPlayer9"),
    Path(r"C:\Program Files (x86)\LDPlayer\LDPlayer9"),
    Path(r"C:\Software\LDPlayer\LDPlayer9"),
    Path(r"D:\LDPlayer\LDPlayer9"),
    Path(r"D:\Program Files\LDPlayer\LDPlayer9"),
    Path(r"D:\Software\LDPlayer\LDPlayer9"),
    Path(r"E:\LDPlayer\LDPlayer9"),
    Path(r"E:\Program Files\LDPlayer\LDPlayer9"),
    Path(r"E:\Software\LDPlayer\LDPlayer9"),
)


def _candidate_ldplayer_dirs() -> tuple[Path, ...]:
    """User-config override (if any) followed by the auto-detect list."""
    from .config import app_config
    cfg = app_config()
    base: tuple[Path, ...] = ()
    if cfg.ldplayer_dir:
        base = (cfg.ldplayer_dir,)
    return base + _KNOWN_LDPLAYER_DIRS


def adb_binary(user_override: str | Path | None = None) -> Path | None:
    """Locate ``adb.exe``.

    Resolution order:
      1. ``user_override`` (passed explicitly by the caller).
      2. ``[paths] ldplayer_dir`` from config.ini.
      3. Built-in LDPlayer install locations.
      4. Bundled ``bin/adb.exe`` (PyInstaller asset).
      5. ``None`` — caller should fall back to ``"adb"`` on PATH.
    """
    if user_override:
        p = Path(user_override).expanduser()
        if p.is_file():
            return p

    for d in _candidate_ldplayer_dirs():
        candidate = d / "adb.exe"
        if candidate.is_file():
            return candidate

    bundled = bundle_dir() / "bin" / "adb.exe"
    if bundled.is_file():
        return bundled

    return None


def ldconsole_binary(user_override: str | Path | None = None) -> Path | None:
    """Locate ``ldconsole.exe``.

    LDPlayer's CLI control tool — never bundled, only lives in the
    user's LDPlayer install. Same search order as ``adb_binary``.
    """
    if user_override:
        p = Path(user_override).expanduser()
        if p.is_file():
            return p
    for d in _candidate_ldplayer_dirs():
        candidate = d / "ldconsole.exe"
        if candidate.is_file():
            return candidate
    return None


def known_ldplayer_dirs() -> tuple[Path, ...]:
    """Read-only view of the built-in search list (useful for GUI hints)."""
    return _KNOWN_LDPLAYER_DIRS


# Kept for backward compatibility with old call sites that imported the
# `_paths` constant under its previous name.
def known_ldplayer_adb_paths() -> tuple[Path, ...]:
    return tuple(d / "adb.exe" for d in _KNOWN_LDPLAYER_DIRS)
