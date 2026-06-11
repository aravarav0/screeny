from __future__ import annotations

import base64
import io
import sys
from dataclasses import dataclass

import mss
from PIL import Image

from screeny.config import SETTINGS


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()


@dataclass(frozen=True)
class Capture:
    image: Image.Image
    width: int
    height: int
    native_width: int
    native_height: int
    monitor_index: int
    full_image: Image.Image | None = None

    def to_base64_png(self, image: Image.Image | None = None) -> str:
        return _encode(image or self.image)


def _encode(image: Image.Image, quality: int | None = None) -> str:
    if quality is None:
        quality = SETTINGS.vision_jpeg_quality
    buffer = io.BytesIO()
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def crop_around(
    capture: Capture,
    cx_norm: float,
    cy_norm: float,
    *,
    frac: float = 0.34,
    out_width: int = 1024,
) -> tuple[str, tuple[int, int, int, int]]:
    """Return (base64 jpeg of a zoomed crop, box) around a normalized point.

    box is (left, top, right, bottom) in native pixel coordinates so the
    refined click can be mapped back to the full screen.
    """
    source = capture.full_image or capture.image
    src_w, src_h = source.size

    half_w = max(1, int(src_w * frac / 2))
    half_h = max(1, int(src_h * frac / 2))
    cx = int(cx_norm * src_w)
    cy = int(cy_norm * src_h)

    left = max(0, min(cx - half_w, src_w - 2 * half_w))
    top = max(0, min(cy - half_h, src_h - 2 * half_h))
    right = min(src_w, left + 2 * half_w)
    bottom = min(src_h, top + 2 * half_h)

    crop = source.crop((left, top, right, bottom))
    if crop.width < out_width:
        scale = out_width / crop.width
        crop = crop.resize(
            (out_width, max(1, int(crop.height * scale))), Image.Resampling.LANCZOS
        )

    # Map crop box back to native coords (source may be native or resized image).
    sx = capture.native_width / src_w
    sy = capture.native_height / src_h
    native_box = (
        int(left * sx),
        int(top * sy),
        int(right * sx),
        int(bottom * sy),
    )
    return _encode(crop), native_box


def crop_native_image(
    capture: Capture,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> tuple[Image.Image, int, int, int, int]:
    """PIL crop in native-pixel coordinates plus offset and crop size."""
    left = max(0, left)
    top = max(0, top)
    right = min(capture.native_width, right)
    bottom = min(capture.native_height, bottom)
    if right - left < 80 or bottom - top < 60:
        raise ValueError("crop region too small")

    source = capture.full_image or capture.image
    src_w, src_h = source.size
    sx = src_w / capture.native_width
    sy = src_h / capture.native_height
    sl = int(left * sx)
    st = int(top * sy)
    sr = max(sl + 1, int(right * sx))
    sb = max(st + 1, int(bottom * sy))
    crop = source.crop((sl, st, sr, sb))
    crop_native_w = right - left
    crop_native_h = bottom - top
    return crop, left, top, crop_native_w, crop_native_h


def crop_native_region(
    capture: Capture,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> tuple[str, int, int, int, int]:
    """JPEG base64 of a native-pixel crop plus offset and crop size."""
    crop, ox, oy, cw, ch = crop_native_image(capture, left, top, right, bottom)
    return _encode(crop), ox, oy, cw, ch


def primary_monitor_size() -> tuple[int, int]:
    """Physical pixel size of the primary monitor (matches pyautogui / UIA)."""
    with mss.mss() as sct:
        mon = sct.monitors[1]
        return int(mon["width"]), int(mon["height"])


def capture_primary_monitor() -> Capture:
    import time

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            return _capture_with_mss()
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.35 * (attempt + 1))
                continue
    if last_error:
        raise last_error
    raise RuntimeError("Screenshot capture failed.")


def _capture_with_mss() -> Capture:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        native_width, native_height = raw.size
        full_image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        image, width, height = _resize_for_vision(full_image)
        return Capture(
            image=image,
            width=width,
            height=height,
            native_width=native_width,
            native_height=native_height,
            monitor_index=1,
            full_image=full_image,
        )


def _resize_for_vision(image: Image.Image) -> tuple[Image.Image, int, int]:
    max_width = SETTINGS.vision_max_width
    width, height = image.size
    if width <= max_width:
        return image, width, height

    scale = max_width / width
    new_size = (max_width, max(1, int(height * scale)))
    resized = image.resize(new_size, Image.Resampling.LANCZOS)
    return resized, new_size[0], new_size[1]
