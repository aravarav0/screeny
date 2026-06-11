"""Point at UI inside a specific window — avoids full-desktop grid confusion."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from screeny.hybrid_pointer import PointerTarget, _score_match
from screeny.ocr_engine import ocr_available, run_ocr

if TYPE_CHECKING:
    from screeny.screen import Capture

_WIZARD_LABELS = ("Install", "Next", "Run", "Finish", "Get Started", "I Agree", "Accept")


def find_in_window(
    query: str,
    capture: Capture,
    rect: tuple[int, int, int, int],
) -> Optional[PointerTarget]:
    """Locate `query` inside a window crop; return native screen coords."""
    from screeny.config import SETTINGS
    from screeny.grid_locator import locate_with_grid
    from screeny.screen import crop_native_image, crop_native_region

    left, top, right, bottom = rect
    try:
        crop_img, ox, oy, cw, ch = crop_native_image(capture, left, top, right, bottom)
    except ValueError:
        return None

    hit = _ocr_in_image(crop_img, query, ox, oy, cw, ch)
    if hit is not None:
        return hit

    if not SETTINGS.use_grid_locator:
        return None

    try:
        b64, _, _, _, _ = crop_native_region(capture, left, top, right, bottom)
    except ValueError:
        return None

    for label in _labels_for(query):
        pt = locate_with_grid(
            label,
            b64,
            cw,
            ch,
            offset_x=ox,
            offset_y=oy,
            single_stage=True,
        )
        if pt is not None:
            return PointerTarget(pt[0], pt[1], label, "grid", 0.6)
    return None


def _labels_for(query: str) -> tuple[str, ...]:
    q = (query or "").strip().lower()
    if "install" in q:
        return ("Install",)
    if "next" in q:
        return ("Next",)
    if "finish" in q:
        return ("Finish",)
    if "run" in q:
        return ("Run",)
    if "download" in q:
        return ("DOWNLOAD", "Download", "Download the game")
    return (query.strip(),) + _WIZARD_LABELS


def _ocr_in_image(
    image,
    query: str,
    offset_x: int,
    offset_y: int,
    crop_w: int,
    crop_h: int,
) -> Optional[PointerTarget]:
    if not ocr_available():
        return None

    try:
        detections = run_ocr(image)
        if not detections:
            return None

        best_score = 0.0
        best_pt: tuple[int, int] | None = None
        best_label = ""
        sx = crop_w / max(image.width, 1)
        sy = crop_h / max(image.height, 1)

        for box, text, conf in detections:
            if conf < 0.55:
                continue
            if len((text or "").strip()) <= 2 and len(query.strip()) > 2:
                continue
            if not _ocr_text_matches(query, text):
                continue
            score = _score_match(query, text)
            if score <= best_score:
                continue
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            cx = int((min(xs) + max(xs)) / 2 * sx) + offset_x
            cy = int((min(ys) + max(ys)) / 2 * sy) + offset_y
            if len(text.strip()) <= 2 and score < 0.95:
                continue
            best_score = score
            best_pt = (cx, cy)
            best_label = text

        if best_pt and best_score >= 0.5:
            return PointerTarget(best_pt[0], best_pt[1], best_label, "ocr", best_score)
    except Exception:
        return None
    return None


def _ocr_text_matches(query: str, text: str) -> bool:
    q = re.sub(r"[^a-z0-9]+", " ", (query or "").lower()).strip()
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if not q or not t:
        return False
    if q == "install" or q.startswith("install "):
        return t == "install" or t == "install now"
    if q in {"download", "download the game"} or q.startswith("download "):
        return t == "download" or t.startswith("download")
    return _score_match(query, text) >= 0.5
