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
import re
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


[league]
# 公會聯賽戰績擷取功能（辨識引擎為 Google Gemini，設定見下方 [gemini]）。
# 每掃一場會另存一份獨立的 Excel 快照檔
# （檔名 league_scores_YYYYMMDD_HHMMSS.xlsx），不會動到上面的裝評工作簿。
# 注意：聯賽與裝評的設定完全分開 — [ocr] 只影響裝評，本區段只影響聯賽。

# 聯賽 Excel 快照檔的輸出資料夾。
# 留空 = 與聯賽名冊 (roster_path) 同目錄。
output_dir =

# 聯賽成員名冊 Excel（欄位：ID、遊戲ID、職業、Last_OCR_ID）。
# 與裝評工作簿完全分開維護；每次掃描會讀取這份名單做比對，
# 並回寫 Last_OCR_ID 欄（其他欄位不會動）。
# 遊戲ID 空白的列會被視為空位、自動略過。
# 留空 = <data 資料夾>/league_scores.xlsx
roster_path =


[gemini]
# 聯賽辨識用的 Gemini 模型名稱。模型版本更新很快，可自行填寫想用的型號。
# 留空 = gemini-3.1-flash-lite（便宜、快、免費額度即可）。
# 名字較難辨識時可改用大一階如 gemini-3-flash-preview。
model =

# Gemini API 金鑰。留空則改讀環境變數 GEMINI_API_KEY。
# 注意：此檔會隨工具一起發佈，填在這裡等於把金鑰分享給所有使用者，
# 且免費額度有每分鐘/每日請求上限，多人同時掃可能會被限流。
api_key =


[google_sheet]
# 「從雲端名冊同步」功能：讀取公會在 Google 試算表上維護的成員名冊
# （編號／遊戲ID／職業 三欄），比對後把退會/新進/改名等變更套用到
# 本機的 guild_scores.xlsx 與 league_scores.xlsx（每筆變更都要人工確認）。
#
# 公會名冊 Google 試算表網址（瀏覽器網址列整串貼上，含 #gid=... 最好）。
# 此網址屬機密資訊，請勿外流給公會以外的人。
# 程式讀取時用的是「按下授權的那位使用者」的 Google 帳號權限 ——
# 該帳號必須看得到這份試算表，否則同步會回報無權限。
sheet_url =

# 應用程式的 OAuth 用戶端（在 Google Cloud Console 建立的「桌面應用程式」
# 用戶端）。第一次同步會開瀏覽器請使用者按「允許」，之後就不會再跳出。
# 留空則改讀環境變數 GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET。
oauth_client_id =
oauth_client_secret =
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
    # League feature: separate snapshot workbook, recognised via Gemini.
    league_output_dir: Path | None = None      # where league_scores_*.xlsx land
    league_roster_path: Path | None = None     # league_scores.xlsx (名冊)
    gemini_model: str | None = None            # e.g. "gemini-3.1-flash-lite"
    gemini_api_key: str | None = None          # or via env GEMINI_API_KEY
    # Roster sync: the guild's member list on Google Sheets.
    gsheet_url: str | None = None              # full browser URL (may carry #gid=)
    gsheet_oauth_client_id: str | None = None      # or env GOOGLE_OAUTH_CLIENT_ID
    gsheet_oauth_client_secret: str | None = None  # or env GOOGLE_OAUTH_CLIENT_SECRET
    # Non-None when config.ini existed but could not be parsed — every
    # field above silently fell back to its default. The GUI surfaces
    # this so the user knows their settings were ignored (a silent
    # fallback here once disabled a user's whole config without a trace).
    load_error: str | None = None

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
        except configparser.Error as exc:
            # A corrupted config shouldn't brick the app — fall back to
            # defaults, but record the error so the GUI can tell the
            # user their settings were ignored (common trigger: a
            # hand-edited duplicate key raising DuplicateOptionError).
            from .logging import logger
            logger.warning("config.ini 解析失敗，全部改用內建預設：{}", exc)
            return cls(load_error=str(exc))

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
            league_output_dir=_path(parser.get("league", "output_dir", fallback="")),
            league_roster_path=_path(parser.get("league", "roster_path", fallback="")),
            gemini_model=_str(parser.get("gemini", "model", fallback="")),
            gemini_api_key=_str(parser.get("gemini", "api_key", fallback="")),
            gsheet_url=_str(parser.get("google_sheet", "sheet_url", fallback="")),
            gsheet_oauth_client_id=_str(
                parser.get("google_sheet", "oauth_client_id", fallback="")
            ),
            gsheet_oauth_client_secret=_str(
                parser.get("google_sheet", "oauth_client_secret", fallback="")
            ),
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
    # Tolerate hand-edited spacing ("key=", "key  =") — matching only the
    # template's "key =" form used to append a DUPLICATE key, which then
    # made configparser reject the whole file on the next load.
    key_re = re.compile(rf"^{re.escape(key)}\s*=")
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
        if in_paths_section and not replaced and key_re.match(stripped):
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
