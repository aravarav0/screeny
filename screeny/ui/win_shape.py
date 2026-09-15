"""Windows rounded overrideredirect window via SetWindowRgn (DPI-aware).

Chroma (-transparentcolor) is unreliable with CustomTkinter on Win11 + 150% scale.
Region coords MUST match the client area from GetClientRect after early DPI init.
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


def _client_size(toplevel: tk.Misc) -> tuple[int, int]:
    """Client area in pixels — must match SetWindowRgn coords."""
    toplevel.update_idletasks()
    w = int(toplevel.winfo_width())
    h = int(toplevel.winfo_height())
    if sys.platform != "win32":
        return max(w, 1), max(h, 1)

    import ctypes
    from ctypes import wintypes

    hwnd = int(toplevel.winfo_id())
    rect = wintypes.RECT()
    if ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
        cw = int(rect.right - rect.left)
        ch = int(rect.bottom - rect.top)
        if cw >= 2 and ch >= 2:
            return cw, ch
    return max(w, 1), max(h, 1)


def _corner_ellipse(w: int, h: int, *, radius_window: int) -> tuple[int, int]:
    """CreateRoundRectRgn ellipse width/height — stadium when bar is short."""
    if h <= radius_window * 2 + 4:
        d = min(w, h)
        return d, d
    d = min(radius_window * 2, w, h)
    return d, d


def _apply_region(toplevel: tk.Misc, *, radius_window: int) -> bool:
    if sys.platform != "win32":
        return False

    import ctypes

    w, h = _client_size(toplevel)
    ew, eh = _corner_ellipse(w, h, radius_window=radius_window)
    hwnd = int(toplevel.winfo_id())
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    user32.SetWindowRgn(hwnd, 0, True)
    rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, ew, eh)
    if not rgn:
        return False
    if not user32.SetWindowRgn(hwnd, rgn, True):
        gdi32.DeleteObject(rgn)
        return False
    return True


def pill_radius(height: int, *, radius_window: int) -> int:
    h = max(height, 1)
    if h <= radius_window * 2 + 4:
        return max(1, h // 2)
    return radius_window


def sync_window_shape(
    toplevel: tk.Misc,
    *,
    chroma: str,
    radius_window: int,
    bg_panel: str,
) -> str:
    """Apply OS-level rounded clip. Returns 'region' or 'none'."""
    del chroma  # reserved; chroma path removed — broken on CTk + 150% DPI
    toplevel.update_idletasks()
    h = max(toplevel.winfo_height(), 1)
    r = pill_radius(h, radius_window=radius_window)

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
        pill.configure(fg_color=bg_panel, corner_radius=r)

    if _apply_region(toplevel, radius_window=radius_window):
        return "region"
    return "none"


def suspend_chroma_for_drag(toplevel: tk.Misc, *, bg_panel: str, radius_window: int) -> None:
    """Keep region during drag — no chroma toggle needed."""
    sync_window_shape(
        toplevel,
        chroma="",
        radius_window=radius_window,
        bg_panel=bg_panel,
    )
