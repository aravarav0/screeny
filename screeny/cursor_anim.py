"""Smooth, visible cursor movement so actions feel human (inspired by Clicky/ThukiWin)."""

from __future__ import annotations

import math
import threading
import time

import pyautogui

from screeny.config import SETTINGS

_ring_window = None
_ring_lock = threading.Lock()


def animate_move(x: int, y: int, *, duration: float | None = None) -> None:
    """Move the real mouse cursor along an eased path."""
    if not SETTINGS.cursor_animate:
        pyautogui.moveTo(x, y)
        return

    duration = duration if duration is not None else SETTINGS.cursor_move_duration
    start_x, start_y = pyautogui.position()
    dx, dy = x - start_x, y - start_y
    dist = math.hypot(dx, dy)
    if dist < 12:
        pyautogui.moveTo(x, y)
        return

    # Longer moves take slightly more time (capped).
    duration = min(max(duration, 0.2), 0.15 + dist / 900.0)
    steps = max(10, min(40, int(duration * 55)))

    for i in range(1, steps + 1):
        t = i / steps
        t = 1.0 - (1.0 - t) ** 2  # ease-out
        nx = int(start_x + dx * t)
        ny = int(start_y + dy * t)
        pyautogui.moveTo(nx, ny, _pause=False)
        time.sleep(duration / steps)


def human_click(
    x: int,
    y: int,
    *,
    button: str = "left",
    clicks: int = 1,
) -> None:
    """Move visibly, pulse ring, then click."""
    animate_move(x, y)
    if SETTINGS.show_click_ring:
        _flash_ring(x, y)
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)


def _flash_ring(x: int, y: int) -> None:
    """Brief topmost ring at click target (best-effort, non-blocking)."""
    try:
        import tkinter as tk
    except ImportError:
        return

    def _show() -> None:
        global _ring_window
        with _ring_lock:
            try:
                if _ring_window is not None:
                    _ring_window.destroy()
            except Exception:
                pass
            root = tk.Tk()
            root.withdraw()
            win = tk.Toplevel(root)
            _ring_window = win
            size = 44
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-transparentcolor", "#010101")
            except tk.TclError:
                pass
            win.configure(bg="#010101")
            canvas = tk.Canvas(
                win,
                width=size,
                height=size,
                bg="#010101",
                highlightthickness=0,
            )
            canvas.pack()
            canvas.create_oval(4, 4, size - 4, size - 4, outline="#3380FF", width=3)
            win.geometry(f"{size}x{size}+{x - size // 2}+{y - size // 2}")
            win.after(450, lambda: (_safe_destroy(win), root.destroy()))

    threading.Thread(target=_show, daemon=True).start()
    time.sleep(0.05)


def _safe_destroy(win) -> None:
    global _ring_window
    try:
        win.destroy()
    except Exception:
        pass
    _ring_window = None
