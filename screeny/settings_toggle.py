"""Flip Windows Settings toggles via UI Automation — no vision model needed."""

from __future__ import annotations

import time

import pyautogui

# How long to wait for the Settings page to render after ms-settings: URI.
_LOAD_WAIT = 1.4
_POLL_TIMEOUT = 5.0

_TOGGLE_TYPES = {"ToggleSwitchControl", "CheckBoxControl", "ButtonControl"}

_LABEL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Bluetooth": ("bluetooth",),
    "Wi‑Fi": ("wi-fi", "wifi", "wi fi"),
    "Airplane mode": ("airplane", "airplane mode"),
    "Night light": ("night light",),
    "Dark mode": ("dark mode",),
    "Display": ("display",),
    "Sound": ("sound", "volume"),
}


def flip_settings_toggle(label: str, intent: str) -> tuple[bool, str]:
    """Find the main toggle for `label` and set on/off/toggle. Returns (ok, message)."""
    try:
        import uiautomation as auto
    except ImportError:
        return False, "UI Automation is not available."

    keywords = _LABEL_KEYWORDS.get(label, (label.lower(),))
    deadline = time.time() + _POLL_TIMEOUT
    time.sleep(_LOAD_WAIT)

    while time.time() < deadline:
        ctrl = _find_main_toggle(auto, keywords)
        if ctrl is not None:
            return _apply_intent(ctrl, label, intent)
        time.sleep(0.35)

    return False, f"Couldn't find the {label} toggle — is the Settings window open?"


def _find_main_toggle(auto, keywords: tuple[str, ...]):
    """Return the best-matching toggle control on the Settings window."""
    try:
        settings = auto.WindowControl(searchDepth=1, Name="Settings")
        if settings.Exists(maxSearchSeconds=0.8):
            best = _best_toggle_in(settings, keywords)
            if best is not None:
                return best
    except Exception:
        pass

    try:
        root = auto.GetRootControl()
        windows = root.GetChildren()
    except Exception:
        return None

    best = None
    best_score = 0
    for win in windows:
        try:
            if win.IsOffscreen:
                continue
            title = (win.Name or "").lower()
            if "screeny" in title:
                continue
        except Exception:
            continue
        candidate = _best_toggle_in(win, keywords)
        if candidate is not None:
            score = _toggle_score(candidate, keywords)
            if score > best_score:
                best_score = score
                best = candidate
    return best


def _best_toggle_in(root, keywords: tuple[str, ...]):
    best = None
    best_score = 0
    for ctrl in _walk(root):
        score = _toggle_score(ctrl, keywords)
        if score > best_score:
            best_score = score
            best = ctrl
    return best


def _toggle_score(ctrl, keywords: tuple[str, ...]) -> int:
    try:
        if ctrl.ControlTypeName not in _TOGGLE_TYPES:
            return 0
        name = (ctrl.Name or "").strip()
        if not name:
            return 0
        low = name.lower()
        if not any(k in low for k in keywords):
            return 0
        score = 10 if any(low == k or low.startswith(k) for k in keywords) else 5
        if ctrl.ControlTypeName == "ToggleSwitchControl":
            score += 3
        return score
    except Exception:
        return 0


def _walk(root, depth: int = 0):
    if depth > 18:
        return
    try:
        children = root.GetChildren()
    except Exception:
        return
    for child in children:
        yield child
        yield from _walk(child, depth + 1)


def _apply_intent(ctrl, label: str, intent: str) -> tuple[bool, str]:
    intent = intent.lower().strip()

    # Try the Toggle pattern first (Settings switches expose this).
    try:
        toggle = ctrl.GetTogglePattern()
        if toggle is not None:
            state = toggle.ToggleState  # 0 = Off, 1 = On
            if intent == "on":
                if state == 1:
                    return True, f"{label} is already on."
                toggle.Toggle()
                return True, f"Turned {label} on."
            if intent == "off":
                if state == 0:
                    return True, f"{label} is already off."
                toggle.Toggle()
                return True, f"Turned {label} off."
            toggle.Toggle()
            return True, f"Toggled {label}."
    except Exception:
        pass

    # Fallback: click the control center.
    try:
        rect = ctrl.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        pyautogui.click(x, y)
        if intent == "on":
            return True, f"Turned {label} on."
        if intent == "off":
            return True, f"Turned {label} off."
        return True, f"Toggled {label}."
    except Exception as exc:
        return False, f"Couldn't click the {label} toggle: {exc}"
