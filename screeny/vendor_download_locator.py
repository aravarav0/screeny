"""OCR-first vendor DOWNLOAD locator — scored candidates, header exclusion, scroll."""

from __future__ import annotations

from dataclasses import dataclass

import re

from screeny.ocr_engine import ocr_available, run_ocr
from screeny.screen import Capture, crop_native_image

INTENT = ("download", "free download", "install now", "get for windows")
BLOCK = (
    "play now",
    "sign in",
    "log in",
    "login",
    "support",
    "downloading",
    "downloaded",
    "about this",
    "get help",
    "get support",
    "download the game",
    "download game",
)
HEADER_FRAC = 0.18
MAX_SCROLLS = 3
MIN_SCORE = 0.8
MAX_BUTTON_WIDTH_FRAC = 0.38


@dataclass
class DownloadCandidate:
    x: int
    y: int
    score: float
    text: str


def _box_metrics(box: list) -> tuple[int, int, int, int]:
    xs = [int(p[0]) for p in box]
    ys = [int(p[1]) for p in box]
    left, top = min(xs), min(ys)
    right, bottom = max(xs), max(ys)
    return left, top, right - left, bottom - top


def score_box(box: tuple[int, int, int, int], vw: int, vh: int, *, text: str = "") -> float:
    x, y, w, h = box
    t = (text or "").strip().lower()
    s = min(h / 60.0, 2.5)
    s += 1.0 - abs((x + w / 2) / max(vw, 1) - 0.5)
    if y > vh * HEADER_FRAC:
        s += 0.5
    else:
        s -= 2.0
    if re.fullmatch(r"download\.?", t):
        s += 2.5
    elif len(t.split()) > 2:
        s -= 1.5
    if w > MAX_BUTTON_WIDTH_FRAC * vw:
        s -= 1.2
    if 25 <= h <= 100 and 70 <= w <= 420:
        s += 0.9
    s += (y / max(vh, 1)) * 0.6
    return s


def find_download_in_detections(
    detections: list[tuple[list, str, float]],
    viewport: tuple[int, int, int, int],
) -> DownloadCandidate | None:
    """Pick best DOWNLOAD OCR hit inside viewport (ltrb screen coords)."""
    left, top, right, bottom = viewport
    vw = max(1, right - left)
    vh = max(1, bottom - top)
    best: DownloadCandidate | None = None
    for box, text, conf in detections:
        if conf < 0.55:
            continue
        t = (text or "").strip().lower()
        if not t or any(b in t for b in BLOCK):
            continue
        if not any(k in t for k in INTENT):
            continue
        bx, by, w, h = _box_metrics(box)
        rel_y = by - top
        if rel_y < 0 or rel_y > vh or bx < left or bx > right:
            continue
        sc = score_box((bx - left, rel_y, w, h), vw, vh, text=t) + conf * 0.2
        if sc < MIN_SCORE:
            continue
        cx = bx + w // 2
        cy = by + h // 2
        if best is None or sc > best.score:
            best = DownloadCandidate(cx, cy, sc, text.strip())
    return best


def ocr_viewport(
    capture: Capture, viewport: tuple[int, int, int, int]
) -> list[tuple[list, str, float]]:
    if not ocr_available():
        return []
    left, top, right, bottom = viewport
    try:
        crop_img, ox, oy, _, _ = crop_native_image(capture, left, top, right, bottom)
    except ValueError:
        return []
    detections = run_ocr(crop_img)
    shifted: list[tuple[list, str, float]] = []
    for box, text, conf in detections:
        shifted_box = [[p[0] + ox, p[1] + oy] for p in box]
        shifted.append((shifted_box, text, conf))
    return shifted


def locate_vendor_download(
    capture: Capture,
    viewport: tuple[int, int, int, int],
) -> DownloadCandidate | None:
    detections = ocr_viewport(capture, viewport)
    return find_download_in_detections(detections, viewport)
