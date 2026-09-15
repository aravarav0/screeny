"""Screeny design tokens — 'Obsidian Command'. Single source of truth.
No hex codes anywhere else in the codebase. Ever."""

import tkinter.font as tkfont


class Color:
    # Chroma fallback only — never used in content
    CHROMA = "#000001"

    # Surface ladder (elevation by lightness, Linear-style)
    BG_0 = "#0A0A0E"      # window / deepest
    BG_1 = "#12121A"      # panel
    BG_2 = "#1A1A26"      # card
    BG_3 = "#232333"      # hover / raised / chip

    STROKE = "#2A2A3A"    # hairline
    STROKE_HI = "#3E3E56" # focus / hover hairline

    TXT_PRIMARY = "#F4F4F8"
    TXT_SECOND = "#A6A6BC"
    TXT_TERT = "#70708C"
    TXT_DISABLED = "#4A4A60"

    # Brand — ember violet, used surgically
    ACCENT = "#8B7CFF"
    ACCENT_HI = "#A89BFF"
    ACCENT_DIM = "#5A4FCC"
    ACCENT_BG = "#221D3D"     # violet-tinted surface (active states)

    # Agent states
    LISTENING = "#5EE7DF"
    WORKING = "#8B7CFF"
    SPEAKING = "#60A5FA"
    ASKING = "#FBBF24"
    SUCCESS = "#4ADE80"
    ERROR = "#F87171"
    OFF = "#3A3A48"

    # Tinted semantic surfaces (cards feel designed, not just outlined)
    LISTENING_BG = "#0F2A28"
    ASKING_BG = "#2A2310"
    SUCCESS_BG = "#102618"
    ERROR_BG = "#2A1518"
    ERROR_STROKE = "#5C2A32"

    ONLINE = "#4ADE80"
    OFFLINE = "#70708C"


# Role chip styling for the activity feed: (label, fg, chip_bg)
ROLE_STYLE = {
    "you":    ("YOU",    Color.LISTENING, Color.LISTENING_BG),
    "plan":   ("PLAN",   Color.ACCENT_HI, Color.ACCENT_BG),
    "think":  ("THINK",  Color.TXT_TERT,  Color.BG_3),
    "act":    ("DO",     Color.ACCENT_HI, Color.ACCENT_BG),
    "screeny":("SCREENY",Color.SPEAKING,  Color.BG_3),
    "ask":    ("ASK",    Color.ASKING,    Color.ASKING_BG),
    "error":  ("ERROR",  Color.ERROR,     Color.ERROR_BG),
    "ok":     ("DONE",   Color.SUCCESS,   Color.SUCCESS_BG),
}


class Space:
    XXS, XS, S, M, L, XL = 2, 4, 8, 12, 16, 24
    RADIUS_WINDOW = 20
    RADIUS_CARD = 12
    RADIUS_INPUT = 10
    RADIUS_CHIP = 999


def _pick(*families: str) -> str:
    try:
        installed = set(tkfont.families())
        for f in families:
            if f in installed:
                return f
    except Exception:
        pass
    return families[-1]


class Font:
    FAMILY = "Segoe UI"            # resolved at runtime by init()
    MONO_FAMILY = "Consolas"
    DISPLAY = TITLE = BODY = CAPTION = MONO = CHIP = None  # set in init()

    @classmethod
    def init(cls) -> None:
        """Call once after the Tk root exists."""
        cls.FAMILY = _pick("Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI")
        cls.MONO_FAMILY = _pick("Cascadia Mono", "Consolas")
        cls.DISPLAY = (cls.FAMILY, 15, "bold")
        cls.TITLE = (cls.FAMILY, 13, "bold")
        cls.BODY = (cls.FAMILY, 12, "normal")
        cls.CAPTION = (cls.FAMILY, 10, "normal")
        cls.CHIP = (cls.FAMILY, 9, "bold")
        cls.MONO = (cls.MONO_FAMILY, 10, "normal")
