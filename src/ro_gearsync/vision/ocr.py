"""Thin wrapper around RapidOCR.

The engine is heavy to initialise (~0.6 s warm, multi-second cold) but stateless
once loaded, so we hide that lifecycle behind a single class and expose a small
typed result wrapper. Callers can feed either an image path or a NumPy array;
the latter is what the row segmenter uses when re-OCRing cropped cells.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from rapidocr import RapidOCR
from rapidocr.utils.typings import ModelType, OCRVersion


@dataclass(frozen=True)
class OcrBox:
    """One detected text box, with the corners in image-pixel coords."""

    text: str
    score: float
    # Four corners (clockwise from top-left): [[x0,y0],[x1,y1],[x2,y2],[x3,y3]]
    polygon: tuple[tuple[float, float], ...]

    @property
    def cx(self) -> float:
        return sum(p[0] for p in self.polygon) / 4

    @property
    def cy(self) -> float:
        return sum(p[1] for p in self.polygon) / 4

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


@dataclass(frozen=True)
class OcrResult:
    boxes: list[OcrBox]

    def in_band(
        self, *, x_range: tuple[float, float] | None = None,
        y_range: tuple[float, float] | None = None,
    ) -> list[OcrBox]:
        """Filter detected boxes by centre-of-mass."""
        out: list[OcrBox] = []
        for b in self.boxes:
            if x_range and not (x_range[0] <= b.cx <= x_range[1]):
                continue
            if y_range and not (y_range[0] <= b.cy <= y_range[1]):
                continue
            out.append(b)
        return out


_MODEL_PRESETS: dict[str, dict[str, object]] = {
    # Default — smallest, fast, OK accuracy. PP-OCRv4 mobile.
    "v4-mobile": {
        "Det.ocr_version": OCRVersion.PPOCRV4, "Det.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV4, "Rec.model_type": ModelType.MOBILE,
    },
    # PP-OCRv4 server. Roughly 5x heavier than mobile, noticeably better on
    # decorative / stylised text.
    "v4-server": {
        "Det.ocr_version": OCRVersion.PPOCRV4, "Det.model_type": ModelType.SERVER,
        "Rec.ocr_version": OCRVersion.PPOCRV4, "Rec.model_type": ModelType.SERVER,
    },
    # PP-OCRv5 mobile — newer architecture, similar footprint to v4 mobile,
    # noticeably stronger on mixed CJK + Latin + punctuation in our domain.
    "v5-mobile": {
        "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
    },
    # PP-OCRv5 server — best accuracy we can reach without leaving RapidOCR.
    "v5-server": {
        "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.SERVER,
        "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.SERVER,
    },
}


class OcrEngine:
    """Reusable RapidOCR handle.

    ``model_quality`` selects a preset (see ``_MODEL_PRESETS``). Server
    presets download on first use (PP-OCRv5 server ~ 200 MB total) and
    are 3-8x slower than mobile, but recover stylised glyphs the mobile
    detector drops.

    **Why cls is disabled by default:** RapidOCR's orientation classifier
    occasionally decides the nickname strip is upside-down and rotates it
    180°. On stylised game digits a rotated ``9`` is pixel-for-pixel
    identical to a ``6``; the cls step is responsible for the long-running
    "999999 reads as 666666 with 1.00 confidence" failure we hunted down.
    The guild member panel never renders rotated text, so cls is pure
    downside in our setup.
    """

    DEFAULT_QUALITY = "v4-mobile"

    def __init__(
        self,
        model_quality: str = DEFAULT_QUALITY,
        *,
        extra_params: dict[str, object] | None = None,
        use_cls: bool = False,
    ) -> None:
        if model_quality not in _MODEL_PRESETS:
            raise ValueError(
                f"unknown model_quality={model_quality!r}; "
                f"choose from {sorted(_MODEL_PRESETS)}"
            )
        self.model_quality = model_quality
        params: dict[str, object] = dict(_MODEL_PRESETS[model_quality])
        params["Global.use_cls"] = use_cls
        if extra_params:
            params.update(extra_params)
        self._engine = RapidOCR(params=params)

    def run(
        self,
        image: str | Path | np.ndarray,
        *,
        text_score: float | None = None,
        box_thresh: float | None = None,
        unclip_ratio: float | None = None,
        use_det: bool | None = None,
        use_rec: bool | None = None,
        use_cls: bool | None = None,
    ) -> OcrResult:
        """Run OCR over an image path or a NumPy array (BGR or RGB).

        The three optional knobs map directly to RapidOCR's detection /
        recognition thresholds. The defaults (None) leave the engine on its
        own settings (text_score=0.5, box_thresh=0.6, unclip_ratio≈1.6).
        Loosening them (lower thresholds, higher unclip) helps recover
        decorative glyphs the detector would otherwise discard — at the
        cost of more spurious boxes.
        """
        kwargs: dict[str, object] = {}
        if text_score is not None:
            kwargs["text_score"] = text_score
        if box_thresh is not None:
            kwargs["box_thresh"] = box_thresh
        if unclip_ratio is not None:
            kwargs["unclip_ratio"] = unclip_ratio
        if use_det is not None:
            kwargs["use_det"] = use_det
        if use_rec is not None:
            kwargs["use_rec"] = use_rec
        if use_cls is not None:
            kwargs["use_cls"] = use_cls
        target = str(image) if isinstance(image, (str, Path)) else image
        raw = self._engine(target, **kwargs)
        return _to_result(raw)


def _to_result(raw: Any) -> OcrResult:
    """Adapt RapidOCR's variant return types into a unified :class:`OcrResult`.

    ``RapidOCR.__call__`` returns one of:
      * ``RapidOCROutput``: full det + rec, has ``boxes`` (polygons),
        ``txts``, and ``scores``. Most common path.
      * ``TextRecOutput``: rec-only (``use_det=False``), has only
        ``txts`` and ``scores`` — no polygon. We synthesize a degenerate
        polygon covering the whole input so downstream code that asks for
        a bbox doesn't crash.
      * ``TextDetOutput`` / ``TextClsOutput``: less common, no text to
        surface — return an empty result.
    """
    boxes: list[OcrBox] = []
    polygons = getattr(raw, "boxes", None)
    texts = getattr(raw, "txts", None)
    scores = getattr(raw, "scores", None)
    if texts is None or scores is None:
        return OcrResult(boxes=[])
    if polygons is None:
        # rec-only path: no polygon, just the recognised string(s).
        for txt, score in zip(texts, scores):
            text = str(txt or "")
            if not text:
                continue
            # Zero-area polygon — callers should treat this as "we don't
            # know where the text was within the crop".
            poly = (
                (0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0),
            )
            boxes.append(
                OcrBox(text=text, score=float(score), polygon=poly)
            )
        return OcrResult(boxes=boxes)
    if len(polygons) == 0:
        return OcrResult(boxes=[])
    for poly, txt, score in zip(polygons, texts, scores):
        pts = tuple((float(p[0]), float(p[1])) for p in poly)
        boxes.append(OcrBox(text=str(txt), score=float(score), polygon=pts))
    return OcrResult(boxes=boxes)
