"""Recognise league screenshots into structured rows.

The recognition layer is abstracted behind :class:`Recognizer` so the rest
of the league pipeline (dedup, roster matching, reconciliation, Excel) is
agnostic to *how* a screenshot becomes rows. The shipped default is
:class:`GeminiRecognizer`; a local-OCR backend can be slotted in later
without touching the callers.

Gemini validated 2026-06-04: structured JSON output (name + the view's four
metrics + 參戰人數) at temperature 0. Numbers come back already converted
from K/M/B to integers, which removes a whole class of brittle parsing the
local pipeline needed.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from ..utils.logging import logger
from .model import VIEW_LABEL, VIEW_METRICS, Battlefield, LeagueRow, Scan, View

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class RecognizerError(RuntimeError):
    """Raised when a backend cannot turn an image into rows."""


class Recognizer(Protocol):
    """Turn one screenshot (BGR ndarray) into a :class:`Scan`.

    ``on_retry`` (optional) is called with a short human-readable message
    whenever the backend hits a transient error and is about to wait+retry
    — lets the GUI explain why a page is taking long. Backends without
    retries may ignore it.
    """

    def recognize(
        self,
        image: np.ndarray,
        battlefield: Battlefield,
        view: View,
        *,
        on_retry=None,
    ) -> Scan: ...


# --------------------------------------------------------------- factory


def create_recognizer(
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> "Recognizer":
    """Build the league recognizer from ``config.ini`` (``[gemini]`` model +
    api_key, or env GEMINI_API_KEY). Explicit arguments override the config
    for tests / CLI flags.

    Gemini is currently the only backend — a local-OCR fallback was
    prototyped (2026-07-02) but cut from the shipped feature set; the
    :class:`Recognizer` protocol stays so one can be re-added without
    touching the pipeline.
    """
    from ..utils.config import app_config
    cfg = app_config()
    return GeminiRecognizer(
        model=model or cfg.gemini_model,
        api_key=api_key or cfg.gemini_api_key,
    )


# --------------------------------------------------------------- key loading


def resolve_api_key(explicit: str | None = None) -> str:
    """Find the Gemini key: explicit arg → config → env → dev key file."""
    if explicit:
        return explicit.strip()
    try:
        from ..utils.config import app_config
        cfg_key = app_config().gemini_api_key
        if cfg_key:
            return cfg_key.strip()
    except Exception:  # noqa: BLE001 — config is best-effort here
        pass
    env = os.environ.get("GEMINI_API_KEY", "").strip()
    if env:
        return env
    # Dev convenience: a gitignored key file at the repo root.
    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        f = base / ".gemini_key"
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
    raise RecognizerError(
        "找不到 Gemini API 金鑰。請在 config.ini 的 [gemini] api_key 填寫，"
        "或設定環境變數 GEMINI_API_KEY。"
    )


# --------------------------------------------------------------- Gemini


class GeminiRecognizer:
    """Vision recognition via the Google Gemini REST API.

    Stateless apart from the resolved key/model; safe to reuse across many
    screenshots in one capture run.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = (model or DEFAULT_GEMINI_MODEL).strip()
        self._key = resolve_api_key(api_key)
        self.timeout = timeout

    # -- prompt / schema ------------------------------------------------

    @staticmethod
    def _prompt(view: View) -> str:
        keys = VIEW_METRICS[view]
        from .model import LABELS
        cols = "、".join(f"{LABELS[k]}" for k in keys)
        view_zh = VIEW_LABEL[view]
        return f"""你是一個精準的遊戲畫面表格辨識器。這是手機遊戲「RO 仙境傳說」的公會聯賽歷史戰績畫面。

畫面分左右兩個表格：**左半邊是我方公會（要辨識）**，右半邊是敵方（完全忽略）。
請**只辨識左半邊我方表格**的每一列。

目前是「{view_zh}」視角，欄位由左到右是：玩家名稱、{cols}。

規則：
1. 只回傳左半邊我方表格的列；右半邊敵方一律不要。
2. 數值帶單位請換算成純整數：K=×1000、M=×1000000、B=×1000000000。
   例：17.2M→17200000、343.3K→343300、1.1B→1100000000、61→61。
3. 找不到的數字（畫面上是空白或 0）請填 0。
4. 忽略任何飄在表格上的浮動通知/撿寶/系統訊息（例如「幽靈劍士娃娃×1」這種不是表格列的字）。
5. **表格最底部若有一列高亮的釘選列（玩家本人的排名複製列），請完全忽略它**——
   它會蓋住底下的列、數字會透出來造成誤讀；本人在列表的正常位置會另外出現。
6. **表格最上方或最下方被裁切、顯示不完整的列，請不要辨識**——它在相鄰截圖會完整出現。
7. name 只要玩家名稱本身，不要包含名字旁的職位圖示。
8. 名字逐字照實辨識，不要自行補字、翻譯或猜測。中文字以繁體為主（如「靜」不是「静」），
   但名字也可能包含英文、日文、韓文、數字或符號——看到什麼就寫什麼，不要轉成中文。
   且有些數字看起來很類似，比如說002跟003就很像，請小心不要辨識錯了。
9. 同時回報畫面左上角的「參戰人數」數字。

請輸出 JSON。"""

    @staticmethod
    def _schema(view: View) -> dict:
        keys = VIEW_METRICS[view]
        props: dict[str, dict] = {"name": {"type": "STRING"}}
        for k in keys:
            props[k] = {"type": "INTEGER"}
        return {
            "type": "OBJECT",
            "properties": {
                "participant_count": {"type": "INTEGER"},
                "rows": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": props,
                        "propertyOrdering": ["name", *keys],
                    },
                },
            },
            "propertyOrdering": ["participant_count", "rows"],
        }

    # -- API call -------------------------------------------------------

    # Transient server-side statuses worth retrying: 429 = our quota /
    # rate limit, 500/503/504 = Google-side hiccups ("model is currently
    # experiencing high demand" comes back as 503 UNAVAILABLE).
    _RETRYABLE_STATUS = frozenset({429, 500, 503, 504})
    _MAX_ATTEMPTS = 5

    def _post_with_retry(self, url: str, body: dict, on_retry=None):
        """POST with exponential backoff (2→4→8→16 s + jitter).

        429 responses sometimes carry the server's own RetryInfo delay —
        when present we honour it instead of our schedule. Non-retryable
        statuses raise immediately. ``on_retry(message)`` fires right
        before each wait so the GUI can explain the stall.
        """
        import random
        import time

        import requests

        delay = 2.0
        last_err = "?"
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                # Key goes in a header, not the query string — URLs end up
                # in proxy/access logs, headers generally don't.
                r = requests.post(
                    url, headers={"x-goog-api-key": self._key}, json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_err = f"連線失敗：{exc}"
            else:
                if r.status_code == 200:
                    return r
                if r.status_code not in self._RETRYABLE_STATUS:
                    raise RecognizerError(
                        f"Gemini API 回傳 {r.status_code}：{r.text[:300]}"
                    )
                last_err = f"HTTP {r.status_code}（{r.text[:120]}）"
                # Server-suggested wait (RetryInfo, e.g. on 429).
                try:
                    for detail in r.json().get("error", {}).get("details", []):
                        if str(detail.get("@type", "")).endswith("RetryInfo"):
                            suggested = float(
                                str(detail.get("retryDelay", "0")).rstrip("s") or 0
                            )
                            delay = max(delay, suggested)
                except (ValueError, AttributeError):
                    pass
            if attempt < self._MAX_ATTEMPTS:
                sleep_s = delay + random.uniform(0.0, 1.0)
                logger.warning(
                    "Gemini 暫時性錯誤（{}）— {:.0f} 秒後重試（第 {}/{} 次）",
                    last_err, sleep_s, attempt + 1, self._MAX_ATTEMPTS,
                )
                if on_retry is not None:
                    try:
                        on_retry(
                            f"Gemini 伺服器忙碌，{sleep_s:.0f} 秒後重試"
                            f"（第 {attempt + 1}/{self._MAX_ATTEMPTS} 次）"
                        )
                    except Exception:  # noqa: BLE001 — UI callback must not break retries
                        pass
                time.sleep(sleep_s)
                delay = min(delay * 2, 60.0)
        raise RecognizerError(
            f"Gemini 重試 {self._MAX_ATTEMPTS} 次仍失敗：{last_err}\n"
            "（通常是官方尖峰過載，稍等幾分鐘再按重新分析即可，已拍的截圖不用重拍）"
        )

    def recognize(
        self,
        image: np.ndarray,
        battlefield: Battlefield,
        view: View,
        *,
        on_retry=None,
    ) -> Scan:
        ok, buf = cv2.imencode(".png", image)
        if not ok:
            raise RecognizerError("failed to encode screenshot to PNG")
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        body = {
            "contents": [{
                "parts": [
                    {"text": self._prompt(view)},
                    {"inline_data": {"mime_type": "image/png", "data": b64}},
                ]
            }],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": self._schema(view),
            },
        }
        url = f"{_API_ROOT}/models/{self.model}:generateContent"
        r = self._post_with_retry(url, body, on_retry=on_retry)
        try:
            data = r.json()
            candidate = data["candidates"][0]
        except (KeyError, IndexError, ValueError) as exc:
            raise RecognizerError(f"Gemini 回傳格式無法解析：{exc}") from exc
        # An abnormal stop (MAX_TOKENS, SAFETY, …) truncates the JSON —
        # surface the reason instead of a cryptic parse error.
        finish = str(candidate.get("finishReason") or "")
        if finish not in ("", "STOP"):
            raise RecognizerError(
                f"Gemini 回應異常中止（finishReason={finish}）。"
                "若為 MAX_TOKENS 代表輸出被截斷，可換用大一階的模型再試。"
            )
        try:
            text = candidate["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except (KeyError, IndexError, ValueError) as exc:
            raise RecognizerError(f"Gemini 回傳格式無法解析：{exc}") from exc

        keys = VIEW_METRICS[view]
        rows: list[LeagueRow] = []
        for raw in parsed.get("rows", []):
            name = str(raw.get("name") or "").strip()
            metrics: dict[str, int | None] = {}
            for k in keys:
                v = raw.get(k)
                metrics[k] = int(v) if isinstance(v, (int, float)) else None
            rows.append(LeagueRow(name=name, metrics=metrics))

        pc = parsed.get("participant_count")
        try:
            participant_count = int(pc) if pc not in (None, "") else None
        except (TypeError, ValueError):
            participant_count = None

        logger.info(
            "league recognise {}/{}: {} rows, 參戰人數={}",
            battlefield, view, len(rows), participant_count,
        )
        return Scan(
            battlefield=battlefield,
            view=view,
            participant_count=participant_count,
            rows=rows,
        )
