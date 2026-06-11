"""Shared RapidOCR instance — loading the model once saves ~1–3s per click."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from PIL import Image

_lock = threading.Lock()
_engine = None


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        return _engine


def ocr_available() -> bool:
    try:
        import numpy  # noqa: F401
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401

        return True
    except ImportError:
        return False


def run_ocr(
    image: Image.Image,
    *,
    max_width: int | None = None,
) -> list[tuple[list, str, float]]:
    """Run OCR; returns list of (box, text, confidence)."""
    from screeny.config import SETTINGS

    import numpy as np
    from PIL import Image

    limit = max_width if max_width is not None else SETTINGS.ocr_max_width
    img = image.convert("RGB")
    if img.width > limit:
        scale = limit / img.width
        img = img.resize(
            (limit, max(1, int(img.height * scale))),
            Image.Resampling.BILINEAR,
        )

    engine = _get_engine()
    result, _ = engine(np.array(img))
    if not result:
        return []

    scale_x = image.width / img.width
    scale_y = image.height / img.height
    out: list[tuple[list, str, float]] = []
    for det in result:
        try:
            box, text, conf = det
        except Exception:
            continue
        scaled_box = [
            [int(p[0] * scale_x), int(p[1] * scale_y)] for p in box
        ]
        out.append((scaled_box, str(text), float(conf)))
    return out
