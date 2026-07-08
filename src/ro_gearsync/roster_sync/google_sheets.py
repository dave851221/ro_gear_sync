"""Read the guild roster sheet via the Google Sheets v4 REST API.

Auth is the OAuth 2.0 **installed-app** flow (loopback redirect + PKCE),
hand-rolled on stdlib + ``requests`` — the whole need here is one consent
dance plus two GET endpoints, so pulling in google-auth /
google-api-python-client would only bloat the PyInstaller bundle.

Flow: first sync opens the user's default browser (where they are already
signed in to Google); they pick an account and click 允許; the browser
redirects to a temporary local HTTP server which captures the auth code;
we exchange it for tokens and cache them at ``data/google_token.json``.
Subsequent runs refresh silently — no browser.

Data access happens **as the authorising user**: the app's OAuth client
carries no permission of its own, so only accounts that can view the
sheet can sync from it (403 otherwise). Scope is spreadsheets.readonly —
the app can never write to the sheet.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import os
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from ..utils.logging import logger
from ..utils.paths import google_token_path

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
# How long we wait for the user to finish the browser consent.
_AUTH_TIMEOUT_S = 300

StatusCb = Callable[[str], None]


class SheetAccessError(RuntimeError):
    """User-facing sheet-access failure (missing config, auth denied,
    no permission on the sheet, bad URL...). Message is display-ready."""


# ------------------------------------------------------------------ URL

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID_RE = re.compile(r"[#?&]gid=(\d+)")


def parse_sheet_url(url: str) -> tuple[str, int | None]:
    """Extract (spreadsheet_id, gid) from a browser URL.

    gid is None when the URL doesn't carry one — the first worksheet is
    used in that case."""
    m = _SHEET_ID_RE.search(url or "")
    if not m:
        raise SheetAccessError(
            "config.ini [google_sheet] sheet_url 看起來不是 Google 試算表網址"
            "（找不到 /spreadsheets/d/<ID>/ 的部分）。"
        )
    gid_m = _GID_RE.search(url)
    return m.group(1), (int(gid_m.group(1)) if gid_m else None)


def configured_sheet_url() -> str:
    from ..utils.config import app_config
    url = (app_config().gsheet_url or "").strip()
    if not url:
        raise SheetAccessError(
            "尚未設定名冊試算表網址。請在 config.ini 的 [google_sheet] "
            "sheet_url 貼上 Google 試算表的完整網址。"
        )
    return url


# ------------------------------------------------------------------ OAuth client credentials

def _oauth_client() -> tuple[str, str]:
    """Resolve the app's OAuth client id/secret.

    Order: config.ini → env GOOGLE_OAUTH_CLIENT_ID/SECRET → a gitignored
    dev file ``.google_oauth_client.json`` at repo root / cwd (the JSON
    downloaded from Google Cloud Console, ``{"installed": {...}}``) —
    same layering as the Gemini key."""
    from ..utils.config import app_config
    cfg = app_config()
    if cfg.gsheet_oauth_client_id and cfg.gsheet_oauth_client_secret:
        return cfg.gsheet_oauth_client_id, cfg.gsheet_oauth_client_secret

    env_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    env_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if env_id and env_secret:
        return env_id, env_secret

    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        f = base / ".google_oauth_client.json"
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                inner = data.get("installed") or data.get("web") or data
                cid = inner.get("client_id", "").strip()
                csec = inner.get("client_secret", "").strip()
                if cid and csec:
                    return cid, csec
            except (json.JSONDecodeError, AttributeError):
                logger.warning("無法解析 {}，忽略", f)
    raise SheetAccessError(
        "找不到 Google OAuth 用戶端設定。請在 config.ini 的 [google_sheet] "
        "填寫 oauth_client_id / oauth_client_secret（或設定環境變數 "
        "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET）。"
    )


# ------------------------------------------------------------------ token cache

def _load_token() -> dict:
    p = google_token_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("google_token.json 損毀，將重新授權")
        return {}


def _save_token(tok: dict) -> None:
    p = google_token_path()
    p.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_token() -> None:
    """Forget the cached authorisation (next sync re-opens the browser)."""
    try:
        google_token_path().unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------ consent flow

class _RedirectHandler(BaseHTTPRequestHandler):
    """Captures ?code=... (or ?error=...) from Google's loopback redirect."""

    # Filled by the server loop.
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if "code" not in params and "error" not in params:
            # favicon.ico etc — not the redirect we're waiting for.
            self.send_response(404)
            self.end_headers()
            return
        type(self).result = {k: v[0] for k, v in params.items()}
        ok = "code" in params
        body = (
            "<html><meta charset='utf-8'><body style='font-family:sans-serif'>"
            + (
                "<h2>✅ 授權完成</h2><p>請關閉此分頁，回到 RO_GearSync。</p>"
                if ok
                else "<h2>❌ 授權未完成</h2><p>你取消了授權。可關閉此分頁，"
                     "回到 RO_GearSync 重試。</p>"
            )
            + "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence per-request stderr spam
        pass


def _run_consent_flow(client_id: str, client_secret: str,
                      status_cb: StatusCb | None = None) -> dict:
    """Open the browser, wait for the user to click 允許, exchange the
    auth code for tokens. Returns the token-endpoint response dict."""
    import requests

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(16)

    # Port 0 = OS-assigned; Google desktop clients accept any loopback port.
    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    server.timeout = 1.0
    _RedirectHandler.result = {}
    redirect_uri = f"http://127.0.0.1:{server.server_port}"

    auth_url = _AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # offline + consent → Google always returns a refresh_token, so
        # the user only ever sees the browser once per machine.
        "access_type": "offline",
        "prompt": "consent",
    })

    if status_cb:
        status_cb("已開啟瀏覽器，請選擇帳號並按「允許」…")
    logger.info("roster sync: opening browser for Google consent")
    if not webbrowser.open(auth_url):
        raise SheetAccessError(
            "無法開啟瀏覽器進行 Google 授權。請確認系統有預設瀏覽器。"
        )

    deadline = time.monotonic() + _AUTH_TIMEOUT_S
    try:
        while time.monotonic() < deadline and not _RedirectHandler.result:
            server.handle_request()
    finally:
        server.server_close()

    result = _RedirectHandler.result
    if not result:
        raise SheetAccessError(
            f"等候授權逾時（{_AUTH_TIMEOUT_S // 60} 分鐘）。請重試一次。"
        )
    if "error" in result:
        raise SheetAccessError(f"Google 授權被拒絕或取消（{result['error']}）。")
    if result.get("state") != state:
        raise SheetAccessError("授權回應驗證失敗（state 不符），請重試。")

    r = requests.post(_TOKEN_ENDPOINT, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if r.status_code != 200:
        raise SheetAccessError(f"兌換授權碼失敗：HTTP {r.status_code}（{r.text[:200]}）")
    return r.json()


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict | None:
    """Silent refresh. None = refresh token revoked/expired → re-consent."""
    import requests

    r = requests.post(_TOKEN_ENDPOINT, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code == 200:
        return r.json()
    logger.warning("Google token refresh 失敗 HTTP {}：{}", r.status_code, r.text[:200])
    return None


def get_access_token(status_cb: StatusCb | None = None) -> str:
    """Return a valid access token, refreshing or re-consenting as needed."""
    client_id, client_secret = _oauth_client()
    tok = _load_token()

    # Cached access token still valid (60 s safety margin)?
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]

    if tok.get("refresh_token"):
        fresh = _refresh(client_id, client_secret, tok["refresh_token"])
        if fresh:
            tok["access_token"] = fresh["access_token"]
            tok["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
            # Google occasionally rotates the refresh token too.
            if fresh.get("refresh_token"):
                tok["refresh_token"] = fresh["refresh_token"]
            _save_token(tok)
            return tok["access_token"]

    fresh = _run_consent_flow(client_id, client_secret, status_cb)
    tok = {
        "access_token": fresh["access_token"],
        "refresh_token": fresh.get("refresh_token", tok.get("refresh_token", "")),
        "expires_at": time.time() + int(fresh.get("expires_in", 3600)),
        "scope": fresh.get("scope", _SCOPE),
    }
    _save_token(tok)
    if status_cb:
        status_cb("授權完成。")
    return tok["access_token"]


# ------------------------------------------------------------------ reads

def _api_get(path: str, token: str, params: dict | None = None):
    import requests

    return requests.get(
        f"{_SHEETS_API}/{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )


def fetch_sheet_grid(url: str | None = None,
                     status_cb: StatusCb | None = None) -> list[list[str]]:
    """Download the roster worksheet as a row-major grid of display strings.

    ``url`` defaults to config.ini's sheet_url. Raises
    :class:`SheetAccessError` with a display-ready message on any failure."""
    sid, gid = parse_sheet_url(url or configured_sheet_url())
    token = get_access_token(status_cb)

    def _get_with_retry(path: str, params: dict | None = None):
        nonlocal token
        r = _api_get(path, token, params)
        if r.status_code == 401:
            # Access token died mid-way (revoked?) — one silent retry
            # through the full token pipeline (refresh or re-consent).
            clear_token()
            token = get_access_token(status_cb)
            r = _api_get(path, token, params)
        return r

    if status_cb:
        status_cb("正在讀取試算表…")
    meta = _get_with_retry(sid, {"fields": "sheets.properties"})
    if meta.status_code == 403:
        raise SheetAccessError(
            "這個 Google 帳號沒有該試算表的檢視權限。請改用有權限的帳號授權"
            "（工具選單可清除已存的授權後重試）。"
        )
    if meta.status_code == 404:
        raise SheetAccessError(
            "找不到試算表 — 請檢查 config.ini [google_sheet] sheet_url 是否正確。"
        )
    if meta.status_code != 200:
        raise SheetAccessError(
            f"讀取試算表資訊失敗：HTTP {meta.status_code}（{meta.text[:200]}）"
        )

    sheets = meta.json().get("sheets", [])
    if not sheets:
        raise SheetAccessError("試算表內沒有任何工作表。")
    title: str | None = None
    if gid is None:
        title = sheets[0]["properties"]["title"]
    else:
        for s in sheets:
            if s["properties"].get("sheetId") == gid:
                title = s["properties"]["title"]
                break
    if title is None:
        raise SheetAccessError(
            f"試算表裡找不到網址指定的工作表（gid={gid}）。"
            "請重新從瀏覽器複製正確分頁的網址。"
        )

    # A1 range = the whole worksheet; single quotes in the title double up.
    a1 = "'" + title.replace("'", "''") + "'"
    vals = _get_with_retry(
        f"{sid}/values/{urllib.parse.quote(a1, safe='')}",
        {"majorDimension": "ROWS"},
    )
    if vals.status_code != 200:
        raise SheetAccessError(
            f"讀取工作表內容失敗：HTTP {vals.status_code}（{vals.text[:200]}）"
        )
    grid = vals.json().get("values", [])
    logger.info("roster sync: fetched {} rows from sheet '{}'", len(grid), title)
    # The API omits trailing empty cells; normalise to str for callers.
    return [[str(c) if c is not None else "" for c in row] for row in grid]
