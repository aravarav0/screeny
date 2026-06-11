"""Fast paths for common Windows system toggles — skip slow Settings navigation."""

from __future__ import annotations

import re
import subprocess
import time

from screeny.settings_toggle import flip_settings_toggle
from screeny.tools import ToolResult

# Deep links straight to the right Settings page (not the Settings home screen).
_SETTINGS_PAGES: dict[str, tuple[str, str]] = {
    "bluetooth": ("ms-settings:bluetooth", "Bluetooth"),
    "wifi": ("ms-settings:network-wifi", "Wi‑Fi"),
    "wi-fi": ("ms-settings:network-wifi", "Wi‑Fi"),
    "wi fi": ("ms-settings:network-wifi", "Wi‑Fi"),
    "airplane mode": ("ms-settings:network-airplanemode", "Airplane mode"),
    "night light": ("ms-settings:nightlight", "Night light"),
    "dark mode": ("ms-settings:colors", "Dark mode"),
    "display": ("ms-settings:display", "Display"),
    "sound": ("ms-settings:sound", "Sound"),
    "volume": ("ms-settings:sound", "Sound"),
}

# If UIA can't find the toggle, vision gets at most this many steps — not 24.
_FALLBACK_VISION_STEPS = 4


def try_system_action(command: str) -> ToolResult | None:
    """Open the right Settings page and flip the toggle directly when possible."""
    text = command.strip().lower()
    if not text:
        return None

    intent = _toggle_intent(text)
    if intent is None:
        return None

    for key, (uri, label) in _SETTINGS_PAGES.items():
        if key not in text:
            continue
        try:
            subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
        except OSError as exc:
            return ToolResult(False, str(exc))

        ok, detail = flip_settings_toggle(label, intent)
        if ok:
            return ToolResult(True, detail)

        # UIA missed — short vision fallback only as last resort.
        if intent == "off":
            action = f"Turn {label} OFF using the main toggle switch."
        elif intent == "on":
            action = f"Turn {label} ON using the main toggle switch."
        else:
            action = f"Toggle the main {label} switch."

        goal = (
            f"{action} The {label} settings page is already open — click the toggle once, "
            f'then return {{"action":"done"}}. Do NOT navigate elsewhere.'
        )
        return ToolResult(
            True,
            f"Opened {label} settings. {detail}",
            goal,
            vision_max_steps=_FALLBACK_VISION_STEPS,
        )

    return None


def _toggle_intent(text: str) -> str | None:
    if re.search(r"\b(turn off|switch off|disable|turn bluetooth off|turn wifi off)\b", text):
        return "off"
    if re.search(r"\b(turn on|switch on|enable|turn bluetooth on|turn wifi on)\b", text):
        return "on"
    if re.search(r"\btoggle\b", text):
        return "toggle"
    return None
