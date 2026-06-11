from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import pyautogui

from screeny.config import SETTINGS
from screeny.cursor_anim import human_click

pyautogui.FAILSAFE = True
pyautogui.PAUSE = SETTINGS.action_pause


class CoordError(ValueError):
    """Raised when click coordinates are missing or invalid (retryable)."""


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    detail: str
    retryable: bool = False


# Map common model variations onto our canonical action names.
ACTION_SYNONYMS = {
    "click": "click",
    "left_click": "click",
    "leftclick": "click",
    "mouse_click": "click",
    "tap": "click",
    "press_left": "click",
    "double_click": "double_click",
    "doubleclick": "double_click",
    "right_click": "right_click",
    "rightclick": "right_click",
    "context_click": "right_click",
    "move": "move",
    "move_to": "move",
    "hover": "move",
    "type": "type",
    "type_text": "type",
    "write": "type",
    "input": "type",
    "enter_text": "type",
    "keypress": "hotkey",
    "key": "hotkey",
    "key_press": "hotkey",
    "press": "hotkey",
    "press_key": "hotkey",
    "hotkey": "hotkey",
    "shortcut": "hotkey",
    "scroll": "scroll",
    "scroll_down": "scroll_down",
    "scroll_up": "scroll_up",
    "wait": "wait",
    "pause": "wait",
    "sleep": "wait",
    "done": "done",
    "finish": "done",
    "complete": "done",
    "success": "done",
    "fail": "fail",
    "abort": "fail",
    "give_up": "fail",
    "unknown": "fail",
    "ask": "ask",
    "ask_user": "ask",
    "request_input": "ask",
    "need_input": "ask",
    "prompt_user": "ask",
    "open_download": "open_download",
    "run_download": "open_download",
    "open_installer": "open_download",
    "run_installer": "open_download",
    "launch_installer": "open_download",
    "open_downloaded_file": "open_download",
}


def normalize_action_name(raw: str) -> str:
    name = str(raw or "").lower().strip().replace("-", "_").replace(" ", "_")
    return ACTION_SYNONYMS.get(name, name)


_BROWSER_BACK_ALIASES = frozenset(
    {"back", "browser_back", "browserback", "go_back", "goback", "navigate_back"}
)
_USELESS_WEB_KEYS = frozenset({"backspace", "delete", "del", "escape", "esc", "tab"})


def parse_hotkey_keys(raw: object) -> list[str]:
    """Normalize model hotkey output; map 'back' -> alt+left."""
    keys = raw
    if isinstance(keys, str):
        keys = [k.strip() for k in re.split(r"[+\s,]+", keys) if k.strip()]
    if not isinstance(keys, list):
        return []
    parsed = [str(k).lower().strip() for k in keys if str(k).strip()]
    if len(parsed) == 1 and parsed[0] in _BROWSER_BACK_ALIASES:
        return ["alt", "left"]
    return parsed


def hotkey_is_useless_for_install(keys: list[str]) -> bool:
    return len(keys) == 1 and keys[0] in _USELESS_WEB_KEYS


def execute(
    action: dict[str, Any],
    *,
    screen_width: int,
    screen_height: int,
    image_width: int | None = None,
    image_height: int | None = None,
) -> ActionResult:
    name = normalize_action_name(action.get("action", ""))
    img_w = image_width or screen_width
    img_h = image_height or screen_height
    coord_kwargs = {
        "image_width": img_w,
        "image_height": img_h,
    }

    if name == "double_click":
        action = {**action, "clicks": 2}
        name = "click"
    if name == "right_click":
        action = {**action, "button": "right"}
        name = "click"
    if name == "scroll_down":
        action = {**action, "amount": -int(abs(_safe_int(action.get("amount"), 600)))}
        name = "scroll"
    if name == "scroll_up":
        action = {**action, "amount": int(abs(_safe_int(action.get("amount"), 600)))}
        name = "scroll"

    if name == "click":
        try:
            x, y = _coords(action, screen_width, screen_height, **coord_kwargs)
        except CoordError as exc:
            return ActionResult(False, str(exc), retryable=True)
        button = str(action.get("button", "left")).lower()
        if button not in {"left", "right", "middle"}:
            button = "left"
        clicks = _safe_int(action.get("clicks"), 1)
        human_click(x, y, button=button, clicks=clicks)
        return ActionResult(True, f"clicked ({x}, {y})")

    if name == "move":
        try:
            x, y = _coords(action, screen_width, screen_height, **coord_kwargs)
        except CoordError as exc:
            return ActionResult(False, str(exc), retryable=True)
        pyautogui.moveTo(x, y)
        return ActionResult(True, f"moved to ({x}, {y})")

    if name == "type":
        text = str(action.get("text", ""))
        if not text:
            return ActionResult(False, "type action had no text", retryable=True)
        if _is_ascii(text):
            pyautogui.typewrite(text, interval=0.02)
        else:
            _type_unicode(text)
        if action.get("enter") or action.get("submit"):
            pyautogui.press("enter")
        return ActionResult(True, f"typed {len(text)} chars")

    if name == "hotkey":
        keys = parse_hotkey_keys(action.get("keys") or action.get("key"))
        if not keys:
            return ActionResult(False, "hotkey missing keys", retryable=True)
        pyautogui.hotkey(*keys)
        return ActionResult(True, f"hotkey {'+'.join(keys)}")

    if name == "scroll":
        amount = _safe_int(action.get("amount"), -600)
        try:
            x, y = _coords(
                action,
                screen_width,
                screen_height,
                default_center=True,
                **coord_kwargs,
            )
        except CoordError:
            x, y = screen_width // 2, screen_height // 2
        pyautogui.scroll(amount, x=x, y=y)
        return ActionResult(True, f"scrolled {amount} at ({x}, {y})")

    if name == "wait":
        seconds = _safe_float(action.get("seconds"), 1.0)
        time.sleep(max(0.0, min(seconds, 5.0)))
        return ActionResult(True, f"waited {seconds}s")

    if name == "open_download":
        since = action.get("_since")
        opened = action.get("_opened")
        since_val = float(since) if since is not None else None
        opened_set = opened if isinstance(opened, set) else None
        return _open_latest_download(since_val, opened=opened_set)

    if name == "done":
        return ActionResult(True, "task marked done")

    if name == "fail":
        reason = str(action.get("reason", "model aborted"))
        return ActionResult(False, reason)

    return ActionResult(
        False,
        f"unknown action '{name}'. Use one of: click, type, hotkey, scroll, wait, "
        "open_download, done, fail.",
        retryable=True,
    )


INSTALLER_EXTS = {".exe", ".msi", ".msix"}
# Partial-download markers various browsers use while a file is still in flight.
PARTIAL_EXTS = {".crdownload", ".part", ".partial", ".tmp", ".download"}


def _download_dirs() -> list:
    from pathlib import Path

    return [Path.home() / "Downloads", SETTINGS.download_dir]


def find_recent_installer(since: float | None = None):
    """Return the newest .exe/.msi downloaded at/after `since` (a Unix time),
    or None. When `since` is None, looks back 20 minutes."""
    cutoff = since if since is not None else time.time() - 20 * 60
    best: tuple[float, object] | None = None
    for directory in _download_dirs():
        try:
            if not directory.exists():
                continue
            for f in directory.iterdir():
                if not f.is_file() or f.suffix.lower() not in INSTALLER_EXTS:
                    continue
                mtime = f.stat().st_mtime
                if mtime >= cutoff and (best is None or mtime > best[0]):
                    best = (mtime, f)
        except OSError:
            continue
    return best[1] if best else None


def download_in_progress(since: float | None = None) -> bool:
    """True if a partial-download file (.crdownload etc.) is still being written."""
    cutoff = (since if since is not None else time.time() - 20 * 60) - 5
    for directory in _download_dirs():
        try:
            if not directory.exists():
                continue
            for f in directory.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() in PARTIAL_EXTS and f.stat().st_mtime >= cutoff:
                    return True
        except OSError:
            continue
    return False


def open_installer(path, *, opened: set[str] | None = None) -> ActionResult:
    """Launch a specific downloaded installer file.

    When `opened` is provided, paths already in the set are skipped so the same
    installer is never launched twice in one task.
    """
    import os

    key = str(path.resolve())
    if opened is not None and key in opened:
        return ActionResult(True, f"installer already opened: {path.name}")

    try:
        os.startfile(str(path))  # noqa: S606
    except OSError as exc:
        return ActionResult(False, f"Couldn't open {path.name}: {exc}")

    if opened is not None:
        opened.add(key)
    return ActionResult(True, f"opened the downloaded installer: {path.name}")


def _open_latest_download(
    since: float | None = None,
    *,
    opened: set[str] | None = None,
) -> ActionResult:
    """Find and launch the most recently downloaded installer.

    Far more reliable than asking vision to click Chrome's download bar. Looks
    for a recent .exe/.msi in the user's Downloads folder (and Screeny's own
    download dir) and runs it directly.
    """
    target = find_recent_installer(since)
    if target is None:
        if download_in_progress(since):
            return ActionResult(
                False,
                "The installer is still downloading. Wait a moment, then try again.",
                retryable=True,
            )
        return ActionResult(
            False,
            "No installer finished downloading yet (.exe/.msi in Downloads). "
            "Wait for the download to complete, then try again.",
            retryable=True,
        )
    return open_installer(target, opened=opened)


def resolve_coords(
    action: dict[str, Any],
    *,
    screen_width: int,
    screen_height: int,
    image_width: int | None = None,
    image_height: int | None = None,
) -> tuple[int, int]:
    """Public helper: resolve a click/point to native screen coords or raise."""
    return _coords(
        action,
        screen_width,
        screen_height,
        image_width=image_width,
        image_height=image_height,
    )


def _coords(
    action: dict[str, Any],
    screen_width: int,
    screen_height: int,
    *,
    image_width: int | None = None,
    image_height: int | None = None,
    default_center: bool = False,
) -> tuple[int, int]:
    img_w = image_width or screen_width
    img_h = image_height or screen_height

    nx = action.get("x_norm", action.get("nx"))
    ny = action.get("y_norm", action.get("ny"))
    if nx is not None and ny is not None:
        fx, fy = _safe_float(nx, -1.0), _safe_float(ny, -1.0)
        if fx > 1.0 or fy > 1.0:
            # Model gave pixels in the x_norm slot; treat as image pixels.
            x = int(round(fx * screen_width / img_w))
            y = int(round(fy * screen_height / img_h))
        else:
            x = int(fx * screen_width)
            y = int(fy * screen_height)
        return _validate_coords(x, y, screen_width, screen_height)

    if "x" in action and "y" in action:
        x = _safe_int(action["x"], -1)
        y = _safe_int(action["y"], -1)
        if img_w != screen_width or img_h != screen_height:
            x = int(round(x * screen_width / img_w))
            y = int(round(y * screen_height / img_h))
        return _validate_coords(x, y, screen_width, screen_height)

    if default_center:
        return screen_width // 2, screen_height // 2

    raise CoordError("no coordinates given — provide x_norm and y_norm (0.0-1.0)")


def _validate_coords(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    if (x, y) == (123, 456):
        raise CoordError(
            "those look like the example numbers — read the real position from the screenshot "
            "and give x_norm/y_norm (0.0-1.0)"
        )
    if x < 0 or y < 0 or x >= width or y >= height:
        # Clamp near-edge coords (grid/OCR drift) instead of failing silently wrong.
        x = max(0, min(x, width - 1))
        y = max(0, min(y, height - 1))
    return x, y


def _safe_int(value: object, default: int) -> int:
    try:
        return int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _type_unicode(text: str) -> None:
    import pyperclip

    previous = pyperclip.paste()
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    pyperclip.copy(previous)
