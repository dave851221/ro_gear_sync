"""User-editable ``config.ini`` next to the .exe (or project root in dev).

Anything that varies per machine (LDPlayer install path, where the user
wants the Excel kept, alternate data/log folders) belongs here instead
of being hard-coded. The file is created with sensible defaults the
first time the app runs so the user always has a template to edit.

Schema:

    [paths]
    ldplayer_dir   = <empty | absolute path>   # adb.exe + ldconsole.exe live here
    workbook_path  = <empty | absolute path>   # main guild_scores.xlsx
    data_dir       = <empty | absolute path>   # captures, backups, OCR fallback cache
    logs_dir       = <empty | absolute path>   # loguru rotating log files

Empty values mean "use the built-in default": auto-detect for LDPlayer,
``<app dir>/data/...`` for the rest.

The config is loaded once at startup via :func:`app_config`. The cache
can be cleared with :func:`reload_config` if the user edits the file
while the app is running.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


# Comments embedded in the default file are how the user discovers what
# the options do. configparser respects ``#`` and ``;`` as comment chars
# when reading, so the template below round-trips cleanly.
_DEFAULT_CONFIG = """\
# RO_GearSync 設定檔
#
# 註解行以 # 或 ; 開頭。值留空表示使用內建預設（路徑相關大多是自動偵測）。
# 修改後請重新啟動 RO_GearSync，或在 GUI 環境檢查面板按「重新偵測」讓變更生效。

[paths]
# LDPlayer 安裝目錄（裡面要有 adb.exe 與 ldconsole.exe）。
# 留空 = 自動搜尋常見路徑：C:\\LDPlayer\\LDPlayer9, C:\\Software\\LDPlayer\\LDPlayer9,
# C:\\Program Files\\LDPlayer\\LDPlayer9, D:\\..., E:\\... 等。
ldplayer_dir =

# 主 Excel 工作簿位置。
# 留空 = <RO_GearSync.exe 所在資料夾>/data/guild_scores.xlsx
workbook_path =

# 擷取資料 (captures/) 與備份 (backups/) 的根目錄。
# 留空 = <RO_GearSync.exe 所在資料夾>/data
data_dir =

# Log 檔目錄。
# 留空 = <RO_GearSync.exe 所在資料夾>/logs
logs_dir =


[ocr]
# 主 OCR 模型（用於每張截圖的 first pass）。
#   v5-mobile  — 預設，快 (~2.6s/頁)、信心平均 ~0.88
#   v5-server  — 慢 (~57s/頁)、信心平均 ~0.92，繁中辨識最強但分析截圖時間翻倍
# 留空 = v5-mobile
primary_model =

# Fallback OCR 模型（用於 primary 信心 < fallback_threshold 的列）。
# 留空 = v5-server。設為 'off' 可完全關閉 fallback。
fallback_model =

# 觸發 fallback 的信心門檻 (0.0 ~ 1.0)。值越高 → 越積極觸發 fallback →
# 整體辨識更準但更慢。預設 0.90（2026-05-22 調整後）。
fallback_threshold =
"""


@dataclass
class AppConfig:
    """Parsed view of ``config.ini``. ``None`` fields = "use default"."""

    ldplayer_dir: Path | None = None
    workbook_path: Path | None = None
    data_dir: Path | None = None
    logs_dir: Path | None = None
    # OCR knobs (None = use built-in default).
    ocr_primary_model: str | None = None       # e.g. "v5-mobile" | "v5-server"
    ocr_fallback_model: str | None = None      # e.g. "v5-server" | "off"
    ocr_fallback_threshold: float | None = None

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """Read the file. If it doesn't exist, write the default template
        and return all-defaults so first-time runs still work."""
        if not path.is_file():
            cls.write_default(path)
            return cls()
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error:
            # A corrupted config shouldn't brick the app — fall back to
            # defaults and let the user fix the file when they get around
            # to it.
            return cls()

        def _path(value: str) -> Path | None:
            value = (value or "").strip()
            return Path(value).expanduser() if value else None

        def _str(value: str) -> str | None:
            value = (value or "").strip()
            return value or None

        def _float(value: str) -> float | None:
            value = (value or "").strip()
            if not value:
                return None
            try:
                return float(value)
            except ValueError:
                return None

        return cls(
            ldplayer_dir=_path(parser.get("paths", "ldplayer_dir", fallback="")),
            workbook_path=_path(parser.get("paths", "workbook_path", fallback="")),
            data_dir=_path(parser.get("paths", "data_dir", fallback="")),
            logs_dir=_path(parser.get("paths", "logs_dir", fallback="")),
            ocr_primary_model=_str(parser.get("ocr", "primary_model", fallback="")),
            ocr_fallback_model=_str(parser.get("ocr", "fallback_model", fallback="")),
            ocr_fallback_threshold=_float(parser.get("ocr", "fallback_threshold", fallback="")),
        )

    @staticmethod
    def write_default(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_CONFIG, encoding="utf-8")


def set_path_value(path: Path, key: str, value: str) -> None:
    """Update a single ``[paths]`` key in config.ini in-place.

    Done as a line-level rewrite (rather than ``configparser.write``)
    so the user-facing comments and section spacing in the file are
    preserved. If the key isn't found, it's appended to the
    ``[paths]`` section; if that section is missing, both are
    appended at the end.

    Use this when the GUI updates a setting on the user's behalf
    (e.g. picking the LDPlayer folder manually). Caller should
    invoke :func:`reload_config` afterwards to pick up the change.
    """
    if not path.is_file():
        AppConfig.write_default(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    in_paths_section = False
    paths_section_seen = False
    key_pattern_prefix = f"{key} ="
    replaced = False
    out_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            # Leaving the previous section. If we were in [paths] and
            # never found the key, drop it in just before the new header.
            if in_paths_section and not replaced:
                out_lines.append(f"{key} = {value}")
                replaced = True
            in_paths_section = stripped == "[paths]"
            if in_paths_section:
                paths_section_seen = True
            out_lines.append(line)
            continue
        if in_paths_section and not replaced and stripped.startswith(key_pattern_prefix):
            out_lines.append(f"{key} = {value}")
            replaced = True
            continue
        out_lines.append(line)

    if not replaced:
        if in_paths_section:
            # File ended while still in [paths]; append at the end.
            out_lines.append(f"{key} = {value}")
        elif paths_section_seen:
            # [paths] existed but had no matching key and we left it
            # before EOF — append at the very end (rare).
            out_lines.append(f"{key} = {value}")
        else:
            # No [paths] section at all — synthesize one.
            out_lines.extend(["", "[paths]", f"{key} = {value}"])

    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


# Module-level cache so each tool path lookup doesn't re-parse the INI.
_CONFIG: AppConfig | None = None


def app_config() -> AppConfig:
    """Lazily load + cache the singleton :class:`AppConfig`.

    Resolves the on-disk location via :func:`paths.config_path` so we
    don't double-import (paths.py imports from here, not the other way).
    """
    global _CONFIG
    if _CONFIG is None:
        # Local import to break the paths ↔ config import cycle.
        from .paths import config_path
        _CONFIG = AppConfig.load(config_path())
    return _CONFIG


def reload_config() -> AppConfig:
    """Drop the cached config and re-read from disk. Returns the new one."""
    global _CONFIG
    _CONFIG = None
    return app_config()
