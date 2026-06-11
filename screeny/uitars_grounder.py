from __future__ import annotations

import os
import re

import httpx

from screeny.config import SETTINGS
from screeny.ollama_client import OllamaError, chat
from screeny.screen import Capture

# Single-step grounding prompt from the UI-TARS docs: it returns ONE point for
# the described target. We let Qwen + our loop do the planning; UI-TARS is used
# purely as a high-accuracy pointer for surfaces the accessibility tree misses.
GROUNDING_PROMPT = (
    "Output only the coordinate of one point in your response. "
    "What element matches the following task: "
)

_COORD_RE = re.compile(r"\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)?")

_available: bool | None = None


def uitars_available(*, force: bool = False) -> bool:
    """True if the UI-TARS model is registered in Ollama (cached)."""
    global _available
    if _available is not None and not force:
        return _available
    if not SETTINGS.use_uitars_fallback:
        _available = False
        return False
    try:
        host = os.environ.get("OLLAMA_HOST", SETTINGS.ollama_host).rstrip("/")
        resp = httpx.get(f"{host}/api/tags", timeout=3.0)
        resp.raise_for_status()
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        target = SETTINGS.uitars_model
        _available = any(
            n == target or n.split(":")[0] == target.split(":")[0] for n in names
        )
    except (httpx.HTTPError, ValueError):
        _available = False
    return _available


def parse_point(text: str) -> tuple[float, float] | None:
    """Extract the first (x, y) pair from a UI-TARS response."""
    if not text:
        return None
    # Strip box markers so the bare numbers are easy to grab.
    cleaned = text.replace("<|box_start|>", "").replace("<|box_end|>", "")
    match = _COORD_RE.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None


def _to_native(
    rx: float, ry: float, capture: Capture, sent_w: int, sent_h: int
) -> tuple[int, int] | None:
    mode = SETTINGS.uitars_coord_mode
    if mode == "auto":
        # >1000 in either axis can't be the 0-1000 scheme.
        mode = "abs" if (rx > 1000 or ry > 1000) else "norm1000"

    if mode == "norm1000":
        nx, ny = rx / 1000.0, ry / 1000.0
    else:  # abs: pixels relative to the image we sent
        nx = rx / sent_w if sent_w else -1
        ny = ry / sent_h if sent_h else -1

    if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
        return None

    x = int(round(nx * capture.native_width))
    y = int(round(ny * capture.native_height))
    x = max(0, min(x, capture.native_width - 1))
    y = max(0, min(y, capture.native_height - 1))
    return x, y


def locate(target: str, capture: Capture) -> tuple[int, int] | None:
    """Ask UI-TARS for the click point of `target`. Returns native (x, y) or None."""
    target = (target or "").strip()
    if not target or not uitars_available():
        return None

    # The 0-1000 scheme is resolution-independent. Prefer the native-resolution
    # capture when available — custom installer buttons are easier to spot.
    image = capture.full_image or capture.image
    sent_w, sent_h = image.size
    max_w = 1600
    if sent_w > max_w:
        ratio = max_w / sent_w
        from PIL import Image

        image = image.resize((max_w, int(sent_h * ratio)), Image.Resampling.LANCZOS)
        sent_w, sent_h = image.size

    try:
        reply = chat(
            model=SETTINGS.uitars_model,
            messages=[
                {
                    "role": "user",
                    "content": GROUNDING_PROMPT + target,
                    "images": [capture.to_base64_png(image)],
                }
            ],
            temperature=0.0,
        )
    except OllamaError:
        return None

    point = parse_point(reply)
    if point is None:
        return None
    return _to_native(point[0], point[1], capture, sent_w, sent_h)
