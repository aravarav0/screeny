from __future__ import annotations

import time

import pyautogui

from screeny.config import SETTINGS

pyautogui.FAILSAFE = True
pyautogui.PAUSE = SETTINGS.action_pause


def open_via_windows_search(query: str) -> tuple[bool, str]:
    text = query.strip()
    if not text:
        return False, "No app name to search for."

    try:
        pyautogui.hotkey("win", "s")
        time.sleep(0.55)
        _type_text(text)
        time.sleep(1.4)
        pyautogui.press("enter")
    except pyautogui.FailSafeException:
        return False, "Emergency stop triggered during Windows search."
    except Exception as exc:
        return False, f"Windows search failed: {exc}"

    return (
        True,
        f"Couldn't find {text} directly, so I searched Windows and opened the best match.",
    )


def _type_text(text: str) -> None:
    if _is_ascii(text):
        pyautogui.typewrite(text, interval=0.03)
        return

    import pyperclip

    previous = pyperclip.paste()
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    pyperclip.copy(previous)


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False
