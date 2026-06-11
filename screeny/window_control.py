from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from screeny.app_finder import find_application, normalize_app_name

BROWSER_EXECUTABLES = {
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "firefox": ("firefox.exe",),
}

BROWSER_ALIASES = {
    "chrome": ("chrome", "google chrome", "google"),
    "edge": ("edge", "microsoft edge"),
    "firefox": ("firefox",),
}

WM_CLOSE = 0x0010
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def close_app(app_name: str) -> tuple[bool, str]:
    label = _display_name(app_name)
    hwnds = _find_windows_for_app(app_name)
    if not hwnds:
        return False, f"{label} does not appear to be open."

    user32 = ctypes.windll.user32
    for hwnd in hwnds:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    if len(hwnds) == 1:
        return True, f"Closed {label}."
    return True, f"Closed {len(hwnds)} {label} windows."


def focus_app(app_name: str) -> bool:
    hwnds = _find_windows_for_app(app_name)
    if not hwnds:
        return False
    return _focus_window(hwnds[0])


def focus_application(app_key: str) -> bool:
    return focus_app(app_key)


def close_application(app_key: str) -> tuple[bool, str]:
    return close_app(app_key)


def resolve_exe_names(app_name: str) -> tuple[str, ...]:
    key = normalize_app_name(app_name)
    for browser_key, aliases in BROWSER_ALIASES.items():
        if key == browser_key or key in aliases:
            return BROWSER_EXECUTABLES[browser_key]

    path = find_application(app_name)
    if path:
        if path.suffix.lower() == ".exe":
            return (path.name.lower(),)
        stem = path.stem.lower()
        return (f"{stem}.exe",)

    compact = key.replace(" ", "")
    return tuple(dict.fromkeys((f"{compact}.exe", f"{key.replace(' ', '')}.exe")))


def _find_windows_for_app(app_name: str) -> list[int]:
    exe_names = resolve_exe_names(app_name)
    hwnds = _find_visible_windows(exe_names)
    if hwnds:
        return hwnds
    return _find_visible_windows_by_title(app_name)


def _find_visible_windows(exe_names: tuple[str, ...]) -> list[int]:
    if sys.platform != "win32":
        return []

    targets = {name.lower() for name in exe_names}
    own_pid = os.getpid()
    found: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        if ctypes.windll.user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True

        pid = _window_process_id(hwnd)
        if pid == own_pid:
            return True

        exe_name = _process_exe_name(pid)
        if exe_name and exe_name.lower() in targets:
            found.append(hwnd)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
    ctypes.windll.user32.EnumWindows(enum_proc, 0)
    return found


def _find_visible_windows_by_title(app_name: str) -> list[int]:
    if sys.platform != "win32":
        return []

    query = normalize_app_name(app_name)
    if not query:
        return []

    own_pid = os.getpid()
    found: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        if ctypes.windll.user32.GetWindow(hwnd, 4):
            return True

        pid = _window_process_id(hwnd)
        if pid == own_pid:
            return True

        title = _window_title(hwnd).lower()
        if not title:
            return True

        if query in title or title.startswith(query):
            found.append(hwnd)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
    ctypes.windll.user32.EnumWindows(enum_proc, 0)
    return found


def _display_name(app_name: str) -> str:
    key = normalize_app_name(app_name)
    if not key:
        return app_name.strip().title()
    return " ".join(part.capitalize() for part in key.split())


def _focus_window(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if user32.GetForegroundWindow() == hwnd:
        return True

    user32.ShowWindow(hwnd, SW_RESTORE)

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
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None

    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)

    return None
