"""Two-stage numbered-grid pointing for any vision LLM (from Clicky universal_locator, MIT)."""

from __future__ import annotations

import base64
import io
import json
import re

from PIL import Image, ImageDraw, ImageFont

from screeny.config import SETTINGS
from screeny.ollama_client import OllamaError, chat

STAGE1_COLS, STAGE1_ROWS = 12, 8
STAGE2_COLS, STAGE2_ROWS = 6, 6
ZOOM_RADIUS = 1

_CELL_RE = re.compile(r"\b(\d{1,3})\b")


def locate_with_grid(
    target: str,
    screenshot_b64: str,
    native_w: int,
    native_h: int,
    *,
    offset_x: int = 0,
    offset_y: int = 0,
    *,
    single_stage: bool = False,
) -> tuple[int, int] | None:
    """Return native pixel center for `target`, or None.

    `native_w`/`native_h` are the native-pixel size of the image in
    `screenshot_b64`. Pass `offset_x`/`offset_y` when the image is a crop
    of a larger screen (e.g. installer window only).
    """
    if not SETTINGS.use_grid_locator:
        return None

    raw = base64.b64decode(screenshot_b64)
    full = Image.open(io.BytesIO(raw)).convert("RGB")
    fw, fh = full.size
    max_w = SETTINGS.grid_max_width
    scale = max_w / fw if fw > max_w else 1.0
    iw, ih = (int(fw * scale), int(fh * scale)) if scale != 1.0 else (fw, fh)
    infer = full.resize((iw, ih), Image.Resampling.LANCZOS) if scale != 1.0 else full

    s1_pick = _ask_grid(infer, target, STAGE1_COLS, STAGE1_ROWS)
    if s1_pick is None:
        return None

    cell_w, cell_h = iw / STAGE1_COLS, ih / STAGE1_ROWS
    idx = s1_pick - 1
    s1_col, s1_row = idx % STAGE1_COLS, idx // STAGE1_COLS

    c0 = max(0, s1_col - ZOOM_RADIUS)
    r0 = max(0, s1_row - ZOOM_RADIUS)
    c1 = min(STAGE1_COLS - 1, s1_col + ZOOM_RADIUS)
    r1 = min(STAGE1_ROWS - 1, s1_row + ZOOM_RADIUS)

    crop = infer.crop(
        (
            int(c0 * cell_w),
            int(r0 * cell_h),
            int((c1 + 1) * cell_w),
            int((r1 + 1) * cell_h),
        )
    )
    if crop.size[0] < 768:
        crop = crop.resize(
            (768, int(crop.size[1] * 768 / max(crop.size[0], 1))),
            Image.Resampling.LANCZOS,
        )

    use_one_stage = single_stage or SETTINGS.grid_single_stage
    s2_pick = None if use_one_stage else _ask_grid(crop, target, STAGE2_COLS, STAGE2_ROWS)
    if s2_pick is None:
        infer_x = (s1_col + 0.5) * cell_w
        infer_y = (s1_row + 0.5) * cell_h
    else:
        s2_idx = s2_pick - 1
        s2_col = s2_idx % STAGE2_COLS
        s2_row = s2_idx // STAGE2_COLS
        s2_cell_w = (c1 - c0 + 1) * cell_w / STAGE2_COLS
        s2_cell_h = (r1 - r0 + 1) * cell_h / STAGE2_ROWS
        infer_x = c0 * cell_w + (s2_col + 0.5) * s2_cell_w
        infer_y = r0 * cell_h + (s2_row + 0.5) * s2_cell_h

    # Map infer-image coords -> native pixels of the (possibly cropped) region.
    nx = int(round(infer_x / iw * native_w)) + offset_x
    ny = int(round(infer_y / ih * native_h)) + offset_y
    return nx, ny


def _ask_grid(img: Image.Image, target: str, cols: int, rows: int) -> int | None:
    grid = _draw_grid(img, cols, rows)
    b64 = _to_b64(grid)
    max_n = cols * rows
    prompt = (
        f'Screenshot with a red numbered grid (cells 1-{max_n}). '
        f'Which ONE cell contains the UI element for: "{target}"? '
        f'Reply ONLY JSON: {{"cell": N}} or {{"cell": 0}} if nothing to click.'
    )
    try:
        text = chat(
            model=SETTINGS.vision_model,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                    "images": [b64],
                },
            ],
            temperature=0.05,
            num_predict=48,
        )
    except OllamaError:
        return None
    return _parse_cell(text, max_n)


def _parse_cell(text: str, max_n: int) -> int | None:
    m = re.search(r"\{[^{}]*\}", text or "")
    if m:
        try:
            obj = json.loads(m.group(0))
            n = int(obj.get("cell", -1))
            if n == 0:
                return None
            if 1 <= n <= max_n:
                return n
        except Exception:
            pass
    for tok in _CELL_RE.findall(text or ""):
        n = int(tok)
        if 1 <= n <= max_n:
            return n
    return None


def _draw_grid(img: Image.Image, cols: int, rows: int) -> Image.Image:
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = base.size
    cw, ch = w / cols, h / rows
    for c in range(1, cols):
        x = int(c * cw)
        draw.line([(x, 0), (x, h)], fill=(255, 0, 0, 180), width=1)
    for r in range(1, rows):
        y = int(r * ch)
        draw.line([(0, y), (w, y)], fill=(255, 0, 0, 180), width=1)
    font = ImageFont.load_default()
    n = 1
    for r in range(rows):
        for c in range(cols):
            draw.rectangle(
                [(int(c * cw) + 2, int(r * ch) + 2), (int(c * cw) + 18, int(r * ch) + 14)],
                fill=(255, 0, 0, 200),
            )
            draw.text((int(c * cw) + 4, int(r * ch) + 2), str(n), fill=(255, 255, 255), font=font)
            n += 1
    return Image.alpha_composite(base, overlay).convert("RGB")


def _to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    q = max(55, SETTINGS.vision_jpeg_quality - 8)
    img.save(buf, format="JPEG", quality=q, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
