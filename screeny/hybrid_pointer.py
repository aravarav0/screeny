"""UIA-first pointing — adapted from Clicky Windows hybrid_pointer.py (MIT License).

Tier 1: Windows UI Automation (foreground window)
Tier 2: Optional OCR (rapidocr-onnxruntime if installed)
Tier 3: Grid vision locator (see grid_locator.py)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("screeny.pointer")

_INTERACTIVE_TYPES = {
    "ButtonControl",
    "HyperlinkControl",
    "ListItemControl",
    "MenuItemControl",
    "MenuItem",
    "TabItemControl",
    "TreeItemControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ToggleSwitchControl",
    "SplitButtonControl",
    "ComboBoxControl",
    "EditControl",
    "ImageControl",
    "Custom",
}


@dataclass
class PointerTarget:
    x: int
    y: int
    label: str
    source: str  # uia | ocr | grid
    confidence: float = 1.0

    @property
    def center_xy(self) -> tuple[int, int]:
        return (self.x, self.y)


def find_target(
    query: str,
    *,
    screenshot_b64: str | None = None,
    screen_w: int | None = None,
    screen_h: int | None = None,
) -> Optional[PointerTarget]:
    """Resolve natural language ('Download button') to screen coordinates."""
    query = (query or "").strip()
    if not query:
        return None

    hit = _find_via_uia(query)
    if hit is not None:
        return hit

    # Full-screen OCR is disabled here — it false-matches taskbar letters ("G"
    # for "Google"). OCR runs only on window crops via window_pointer.py.

    if screenshot_b64 and screen_w and screen_h:
        try:
            from screeny.grid_locator import locate_with_grid

            pt = locate_with_grid(
                query,
                screenshot_b64,
                screen_w,
                screen_h,
            )
            if pt is not None:
                return PointerTarget(pt[0], pt[1], query, "grid", 0.55)
        except Exception as exc:
            log.debug("grid locator failed: %s", exc)

    return None


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _score_match(query: str, name: str) -> float:
    q, n = _normalize(query), _normalize(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    # Short wizard words must not match longer labels ("Install" vs "Install YouTube").
    if len(q.split()) == 1 and len(q) <= 8 and q in {"install", "next", "run", "finish"}:
        if n == q:
            return 0.95
        if q == "install" and re.fullmatch(r"install(\.| now)?", n):
            return 0.95
        return 0.0
    # Never match a 1–2 char OCR blob to a longer query ("G" ≠ "Google").
    if len(n) <= 2 and len(q) > 2:
        return 0.0
    if len(q) <= 2 and len(n) > 2:
        return 0.0
    if q in n or n in q:
        return 0.88
    q_words = set(q.split())
    n_words = set(n.split())
    if q_words and n_words:
        return len(q_words & n_words) / max(len(q_words), 1) * 0.75
    return 0.0


def _screen_size() -> tuple[int, int]:
    try:
        import mss

        with mss.mss() as sct:
            mon = sct.monitors[1]
            return int(mon["width"]), int(mon["height"])
    except Exception:
        return 1920, 1080


def _in_taskbar_zone(x: int, y: int, width: int, height: int) -> bool:
    if y < 0 or x < 0 or y >= height or x >= width:
        return True
    if y >= height * 0.9:
        return True
    if x >= width * 0.82 and y >= height * 0.82:
        return True
    return False


def _find_via_uia(query: str, min_score: float = 0.48) -> Optional[PointerTarget]:
    try:
        import uiautomation as auto
    except ImportError:
        return None

    try:
        root = auto.GetForegroundControl() or auto.GetRootControl()
    except Exception:
        return None

    best_score = 0.0
    best = None
    sw, sh = _screen_size()
    queue = [(root, 0)]
    visited = 0

    while queue and visited < 3200:
        node, depth = queue.pop(0)
        visited += 1
        try:
            name = node.Name or ""
            ctrl_type = node.ControlTypeName or ""
            rect = node.BoundingRectangle
        except Exception:
            continue
        if rect and rect.width() > 0 and rect.height() > 0:
            score = _score_match(query, name)
            if score < min_score or score <= best_score:
                continue
            cx = int((rect.left + rect.right) // 2)
            cy = int((rect.top + rect.bottom) // 2)
            if _in_taskbar_zone(cx, cy, sw, sh):
                continue
            best_score = score
            best = (node, rect, name or ctrl_type)
        if depth < 38:
            try:
                for child in node.GetChildren():
                    queue.append((child, depth + 1))
            except Exception:
                pass

    if not best:
        return None

    _, rect, label = best
    cx = int((rect.left + rect.right) // 2)
    cy = int((rect.top + rect.bottom) // 2)
    if _in_taskbar_zone(cx, cy, sw, sh):
        return None
    return PointerTarget(cx, cy, label, "uia", best_score)


def _find_via_ocr(query: str) -> Optional[PointerTarget]:
    from screeny.ocr_engine import ocr_available, run_ocr

    if not ocr_available():
        return None

    try:
        import mss
        from PIL import Image
    except ImportError:
        return None

    try:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        detections = run_ocr(img)
        if not detections:
            return None
        best_score = 0.0
        best_pt = None
        best_label = ""
        for box, text, conf in detections:
            if conf < 0.4:
                continue
            score = _score_match(query, text)
            if score > best_score:
                xs = [int(p[0]) for p in box]
                ys = [int(p[1]) for p in box]
                best_score = score
                best_pt = ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2)
                best_label = text
        if best_pt and best_score >= 0.45:
            return PointerTarget(
                best_pt[0], best_pt[1], best_label, "ocr", best_score
            )
    except Exception as exc:
        log.debug("OCR tier: %s", exc)
    return None
