"""Rounded overlay on Windows.

CustomTkinter draws opaque rectangular canvases, so Tk `-transparentcolor`
alone looks square. We do two things on the *same* outer HWND:

1. SetWindowRgn from that HWND's GetClientRect (no DPI guesswork)
2. WS_EX_LAYERED color-key + canvas backgrounds set to CHROMA so CTk
   rounded-frame corners actually punch through
"""

from __future__ import annotations

import sys
import tkinter as tk


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _hex_to_colorref(hex_color: str) -> int:
    raw = hex_color.lstrip("#")
    r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    return r | (g << 8) | (b << 16)


def _outer_hwnd(toplevel: tk.Misc) -> int:
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = int(toplevel.winfo_id())
    parent = int(user32.GetParent(hwnd) or 0)
    return parent or hwnd


def _hwnd_client_size(hwnd: int) -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    if ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return max(int(rect.right), 1), max(int(rect.bottom), 1)
    return 1, 1


def pill_radius(height: int, *, radius_window: int) -> int:
    h = max(height, 1)
    if h <= radius_window * 2 + 4:
        return max(1, h // 2)
    return radius_window


def _paint_chroma_canvases(toplevel: tk.Misc, chroma: str) -> None:
    """Only the window/pill canvases — never StatusOrb / journey canvases."""
    for widget in (toplevel, getattr(toplevel, "pill", None)):
        if widget is None:
            continue
        canvas = getattr(widget, "_canvas", None)
        if canvas is None:
            continue
        try:
            canvas.configure(bg=chroma, highlightthickness=0)
        except tk.TclError:
            pass


def _apply_color_key(hwnd: int, chroma: str) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    gwl_exstyle = -20
    ws_ex_layered = 0x00080000
    lwa_colorkey = 0x00000001
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    style = get_long(hwnd, gwl_exstyle)
    set_long(hwnd, gwl_exstyle, style | ws_ex_layered)
    user32.SetLayeredWindowAttributes(hwnd, _hex_to_colorref(chroma), 0, lwa_colorkey)


def _apply_region(hwnd: int, w: int, h: int, *, radius_window: int) -> bool:
    import ctypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    ellipse = min(w, h) if h <= radius_window * 2 + 4 else min(radius_window * 2, w, h)
    user32.SetWindowRgn(hwnd, 0, True)
    rgn = gdi32.CreateRoundRectRgn(0, 0, w, h, ellipse, ellipse)
    if not rgn:
        return False
    if not user32.SetWindowRgn(hwnd, rgn, True):
        gdi32.DeleteObject(rgn)
        return False
    return True


def sync_window_shape(
    toplevel: tk.Misc,
    *,
    chroma: str,
    radius_window: int,
    bg_panel: str,
) -> str:
    toplevel.update_idletasks()
    h = max(int(toplevel.winfo_height()), 1)
    r = pill_radius(h, radius_window=radius_window)

    toplevel.configure(fg_color=chroma)
    pill = getattr(toplevel, "pill", None)
    if pill is not None:
        pill.configure(fg_color=bg_panel, corner_radius=r)

    _paint_chroma_canvases(toplevel, chroma)

    if sys.platform != "win32":
        return "none"

    try:
        hwnd = _outer_hwnd(toplevel)
        cw, ch = _hwnd_client_size(hwnd)
        _apply_color_key(hwnd, chroma)
        region_ok = _apply_region(hwnd, cw, ch, radius_window=radius_window)
        try:
            toplevel.attributes("-transparentcolor", chroma)
        except tk.TclError:
            pass
        return "region+chroma" if region_ok else "chroma"
    except Exception:
        return "none"


def suspend_chroma_for_drag(toplevel: tk.Misc, *, bg_panel: str, radius_window: int) -> None:
    """Solid fill while dragging — layered color-key smears under motion."""
    if sys.platform == "win32":
        try:
            import ctypes

            hwnd = _outer_hwnd(toplevel)
            ctypes.windll.user32.SetWindowRgn(hwnd, 0, True)
        except Exception:
            pass
    try:
        toplevel.attributes("-transparentcolor", "")
    except tk.TclError:
        pass
    toplevel.configure(fg_color=bg_panel)
    canvas = getattr(toplevel, "_canvas", None)
    if canvas is not None:
        try:
            canvas.configure(bg=bg_panel)
        except tk.TclError:
            pass
    pill = getattr(toplevel, "pill", None)
    if pill is not None:
        r = pill_radius(max(int(toplevel.winfo_height()), 1), radius_window=radius_window)
        pill.configure(fg_color=bg_panel, corner_radius=r)
