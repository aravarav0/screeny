from __future__ import annotations

import re
import time

import pyautogui

from screeny.app_finder import find_application
from screeny.config import SETTINGS
from screeny.tools import ToolResult, launch_app
from screeny.window_control import close_application, focus_app

pyautogui.FAILSAFE = True
pyautogui.PAUSE = SETTINGS.action_pause

BROWSER_NAMES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
}


def try_browser_action(command: str) -> ToolResult | None:
    text = command.strip().lower()
    if not text:
        return None

    if not re.search(r"\b(close|closing|shut)\b", text):
        return None

    if should_use_vision_for_browser(command):
        return None

    if _wants_close_all_tabs(text):
        browser = _detect_browser(text)
        return _close_all_tabs(browser)

    if _wants_close_single_tab(text):
        browser = _detect_browser(text)
        return _close_current_tab(browser)

    if _wants_close_app(text):
        browser = _detect_browser(text)
        if browser:
            return _close_app(browser)

    return None


def should_use_vision_for_browser(command: str) -> bool:
    """Use vision when the user points at UI on screen instead of bulk shortcuts."""
    text = command.strip().lower()
    if not re.search(r"\b(close|closing|shut|click|press|select|switch)\b", text):
        return False
    if not re.search(r"\b(tab|tabs|chrome|edge|firefox|browser|window|button|link|icon)\b", text):
        return False

    if _wants_close_all_tabs(text):
        return False

    if _wants_close_single_tab(text):
        disambiguation = (
            r"\b(on screen|on the screen|on my screen|that tab|named|called|titled|"
            r"with .+ in|not current|background|other tab|second tab|left tab|right tab|"
            r"gemini|youtube|netflix|gmail|reddit|facebook|twitter|discord)\b"
        )
        return bool(re.search(disambiguation, text))

    visual_cues = (
        r"\b(on screen|on the screen|on my screen|visible|here|you see|shown|"
        r"looking at|named|called|titled|click the|press the)\b"
    )
    if re.search(visual_cues, text):
        return True

    if re.search(r"\b(click|press)\b", text) and re.search(
        r"\b(x|button|tab|close|icon|link)\b", text
    ):
        return True

    return False


def _wants_close_single_tab(text: str) -> bool:
    if not re.search(r"\btab\b", text):
        return False
    if _wants_close_all_tabs(text):
        return False
    if _wants_close_app(text):
        return False
    return bool(re.search(r"\b(close|closing|shut)\b", text))


def _wants_close_all_tabs(text: str) -> bool:
    if not re.search(r"\b(tab|tabs)\b", text):
        return False
    return bool(
        re.search(r"\b(all|every|each)\b", text)
        or re.search(r"\bclose\s+(all\s+)?tabs\b", text)
        or re.search(r"\btabs\b", text) and not re.search(r"\btab\b", text)
    )


def _wants_close_app(text: str) -> bool:
    return bool(re.search(r"\b(close|quit|exit)\b", text)) and bool(
        re.search(r"\b(chrome|edge|firefox|browser|google)\b", text)
    ) and not re.search(r"\b(tab|tabs)\b", text)


def _detect_browser(text: str) -> str:
    for name in sorted(BROWSER_NAMES, key=len, reverse=True):
        if name in text:
            return BROWSER_NAMES[name]
    return "chrome"


def _close_all_tabs(browser: str) -> ToolResult:
    _focus_browser(browser)
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "shift", "w")
    label = browser.title()
    return ToolResult(True, f"Closed all tabs in {label}.")


def _close_current_tab(browser: str) -> ToolResult:
    _focus_browser(browser)
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "w")
    label = browser.title()
    return ToolResult(True, f"Closed the current tab in {label}.")


def _close_app(browser: str) -> ToolResult:
    ok, message = close_application(browser)
    return ToolResult(ok, message)


def _focus_browser(browser: str) -> None:
    if focus_app(browser):
        time.sleep(0.3)
        return
    if find_application(browser):
        launch_app(browser)
        time.sleep(1.0)
        focus_app(browser)
        time.sleep(0.3)
        return
    launch_app(browser)
    time.sleep(1.0)
    focus_app(browser)
    time.sleep(0.3)
