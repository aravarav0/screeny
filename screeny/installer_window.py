"""Detect and focus setup/installer windows (Chrome may auto-run downloads)."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes

# Strong signals — generic "launcher" alone is NOT enough (Steam/Epic false positives).
_TITLE_STRONG = re.compile(
    r"\b(setup|installer|installing|preparing to install|"
    r"riot client|install valorant|valorant|welcome to .+ setup|"
    r"user account control|allow .+ to make changes)\b",
    re.I,
)

_SKIP_EXE = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "vivaldi.exe",
        "explorer.exe",
        "python.exe",
        "pythonw.exe",
        "windowsterminal.exe",
        "cmd.exe",
        "powershell.exe",
        "cursor.exe",
        "code.exe",
    }
)

# Game store clients — only count if the *title* says setup/install.
_LAUNCHER_EXE = frozenset(
    {
        "steam.exe",
        "epicgameslauncher.exe",
        "eadesktop.exe",
        "galaxyclient.exe",
        "ubisoftconnect.exe",
        "battle.net.exe",
        "riotclientservices.exe",
        "riotclientux.exe",
    }
)

_SETUP_EXE = re.compile(
    r"(setup|installer|install(?![a-z])|msiexec|valorant.*\.exe)",
    re.I,
)

MIN_SETUP_SCORE = 14


def focus_setup_window_if_present(*, min_score: int = MIN_SETUP_SCORE) -> str | None:
    """Bring a setup/installer window to the foreground. Returns title or None."""
    if sys.platform != "win32":
        return None
    match = _best_setup_window(min_score=min_score)
    if match is None:
        return None
    score, hwnd, title = match
    if score < min_score:
        return None
    if _focus_window(hwnd):
        return title
    return title


def setup_window_visible(*, min_score: int = MIN_SETUP_SCORE) -> bool:
    return _best_setup_window(min_score=min_score) is not None


def get_foreground_window_rect() -> tuple[int, int, int, int] | None:
    """Native-pixel bounds of the foreground window (left, top, right, bottom)."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w < 120 or h < 80:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _best_setup_window(*, min_score: int = 0) -> tuple[int, int, str] | None:
    own_pid = os.getpid()
    best: tuple[int, int, str] | None = None

    def callback(hwnd: int, _lparam: int) -> bool:
        nonlocal best
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        if ctypes.windll.user32.GetWindow(hwnd, 4):
            return True

        pid = _window_process_id(hwnd)
        if pid == own_pid:
            return True

        title = _window_title(hwnd)
        if not title or len(title) < 3:
            return True

        exe = _process_exe_name(pid) or ""
        if exe.lower() in _SKIP_EXE:
            return True

        score = _score_setup_window(title, exe)
        if score >= min_score and (best is None or score > best[0]):
            best = (score, hwnd, title)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
        callback
    )
    ctypes.windll.user32.EnumWindows(enum_proc, 0)
    return best


def _score_setup_window(title: str, exe: str) -> int:
    low_title = title.lower()
    low_exe = exe.lower()

    if low_exe in _LAUNCHER_EXE:
        if not re.search(r"\b(setup|install|installer)\b", low_title):
            return 0

    score = 0
    if _TITLE_STRONG.search(title):
        score += 16
    elif re.search(r"\b(setup|install|installer)\b", low_title):
        score += 12

    if _SETUP_EXE.search(low_exe):
        score += 10

    if "google" in low_title or "search" in low_title:
        score -= 30
    if re.search(r"\b(steam|epic games|library|store|connect)\b", low_title):
        if not re.search(r"\b(setup|install)\b", low_title):
            score -= 15

    return score


def _focus_window(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if user32.GetForegroundWindow() == hwnd:
        return True

    user32.ShowWindow(hwnd, 9)

    foreground = user32.GetForegroundWindow()
    if foreground:
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()

        user32.AttachThreadInput(current_thread, foreground_thread, True)
        user32.AttachThreadInput(target_thread, foreground_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(target_thread, foreground_thread, False)
        user32.AttachThreadInput(current_thread, foreground_thread, False)
    else:
        user32.SetForegroundWindow(hwnd)

    return user32.GetForegroundWindow() == hwnd


def _window_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_title(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _process_exe_name(pid: int) -> str | None:
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return None
