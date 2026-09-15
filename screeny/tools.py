from __future__ import annotations

import re
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from urllib.parse import quote_plus

from screeny.app_finder import launch_application, normalize_app_name
from screeny.context import SESSION
from screeny.window_control import close_app
from screeny.windows_search import open_via_windows_search


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str
    vision_goal: str | None = None
    vision_max_steps: int | None = None


APP_ALIASES: dict[str, list[str]] = {
    "spotify": ["spotify:", "spotify"],
    "chrome": ["chrome"],
    "edge": ["msedge"],
    "firefox": ["firefox"],
    "notepad": ["notepad"],
    "calculator": ["calc"],
    "settings": ["ms-settings:"],
    "file explorer": ["explorer"],
    "explorer": ["explorer"],
}

# phrase -> (url, success message, optional vision follow-up)
SMART_URLS: dict[str, tuple[str, str, str | None]] = {
    "youtube analytics": (
        "https://studio.youtube.com/",
        "Opened YouTube Studio.",
        "In YouTube Studio, click Analytics in the left sidebar.",
    ),
    "youtube studio": ("https://studio.youtube.com/", "Opened YouTube Studio.", None),
    "yt studio": ("https://studio.youtube.com/", "Opened YouTube Studio.", None),
    "gmail": ("https://mail.google.com/", "Opened Gmail.", None),
    "my email": ("https://mail.google.com/", "Opened Gmail.", None),
    "google drive": ("https://drive.google.com/", "Opened Google Drive.", None),
    "google docs": ("https://docs.google.com/", "Opened Google Docs.", None),
    "github": ("https://github.com/", "Opened GitHub.", None),
    "netflix": ("https://www.netflix.com/", "Opened Netflix.", None),
    "twitter": ("https://x.com/", "Opened X.", None),
    "x.com": ("https://x.com/", "Opened X.", None),
    "instagram": ("https://www.instagram.com/", "Opened Instagram.", None),
    "insta": ("https://www.instagram.com/", "Opened Instagram.", None),
    "facebook": ("https://www.facebook.com/", "Opened Facebook.", None),
    "tiktok": ("https://www.tiktok.com/", "Opened TikTok.", None),
    "linkedin": ("https://www.linkedin.com/", "Opened LinkedIn.", None),
    "whatsapp": ("https://web.whatsapp.com/", "Opened WhatsApp Web.", None),
}

_BROWSER_IN = re.compile(
    r"\b(?:on|in)\s+(?:(?:google\s+)?chrome|edge|firefox|(?:the\s+)?browser)\b",
    re.I,
)

_SITE_ALIASES: dict[str, str] = {
    "instagram": "https://www.instagram.com/",
    "insta": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "fb": "https://www.facebook.com/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "tiktok": "https://www.tiktok.com/",
    "linkedin": "https://www.linkedin.com/",
    "reddit": "https://www.reddit.com/",
    "whatsapp": "https://web.whatsapp.com/",
    "discord": "https://discord.com/app",
    "gmail": "https://mail.google.com/",
    "youtube": "https://www.youtube.com/",
}


def try_smart_intent(command: str) -> ToolResult | None:
    text = command.strip().lower()
    if not text:
        return None

    for phrase, (url, message, vision_goal) in SMART_URLS.items():
        if phrase in text:
            webbrowser.open(url)
            SESSION.note_url(url)
            return ToolResult(True, message, vision_goal)

    yt = parse_youtube_intent(command)
    if yt:
        return yt

    search = _extract_google_search(text)
    if search and not _is_ui_control_command(text):
        return google_search(search)

    return None


def try_close_app(command: str) -> ToolResult | None:
    text = command.strip().lower()
    if not re.search(r"\b(close|closing|shut|quit|exit)\b", text):
        return None

    if re.search(r"\b(tab|tabs)\b", text):
        return None

    target = _extract_close_target(text)
    if not target:
        return None

    vague = {
        "it",
        "this",
        "that",
        "window",
        "app",
        "application",
        "program",
        "software",
    }
    if target in vague:
        return None

    ok, message = close_app(target)
    return ToolResult(ok, message)


def _extract_close_target(text: str) -> str | None:
    patterns = (
        r"\b(?:can you|could you|would you|please)\s+(?:close|quit|exit|shut)\s+(?:the|my|this|that|)\s*(.+?)(?:\s+now|\s+please|\s+for me|\.|$)",
        r"\b(?:close|closing|shut|quit|exit|kill|stop)\s+(?:the|my|this|that|out\s+of|)\s*(?:app\s+)?(.+?)(?:\s+now|\s+please|\s+for me|\.|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            target = _clean_close_target(match.group(1))
            if target:
                return target
    return None


def _clean_close_target(raw: str) -> str:
    target = raw.strip().lower().rstrip(".?!,")
    target = re.sub(r"\s+(now|please|for me|thanks)[.?!]?$", "", target)
    target = re.sub(r"^(the|my|a|an)\s+", "", target)
    target = re.sub(r"\s+like\s+close\s+.+\.?$", "", target)
    return target.strip().rstrip(".?!,")


def _strip_browser_phrases(text: str) -> str:
    out = _BROWSER_IN.sub("", text).strip()
    out = re.sub(r"\s{2,}", " ", out)
    return out.rstrip(".?!,")


def _detect_browser_from_text(text: str) -> str:
    m = re.search(
        r"\b(?:on|in)\s+(?:(?:google\s+)?(chrome|edge|firefox)|(?:the\s+)?browser)\b",
        text,
        re.I,
    )
    if not m:
        return "chrome"
    browser = m.group(1).lower() if m.group(1) else "chrome"
    return browser if browser in {"chrome", "edge", "firefox"} else "chrome"


def _site_url(site_name: str) -> str | None:
    key = normalize_app_name(site_name)
    if key in _SITE_ALIASES:
        return _SITE_ALIASES[key]
    if key.endswith((".com", ".org", ".net", ".io", ".gg")):
        return key if key.startswith("http") else f"https://{key}"
    if "." not in key and " " not in key and len(key) >= 3:
        return f"https://www.{key}.com/"
    return None


def _needs_web_followthrough(text: str) -> bool:
    return bool(
        re.search(
            r"\b(message|dm|text|send|chat|reply|post|sign in|log in|login|"
            r"download|install|click|navigate|search)\b",
            text,
            re.I,
        )
    )


def try_open_in_browser(command: str) -> ToolResult | None:
    """Parse 'open instagram on chrome and message …' — not a Windows app search."""
    text = command.strip().lower()
    if not _BROWSER_IN.search(text):
        return None
    if not re.search(r"\b(?:open|launch|go to|visit)\b", text):
        return None

    browser = _detect_browser_from_text(text)
    site_text = _strip_browser_phrases(text)
    site_text = re.sub(r"^(?:open|launch|go to|visit)\s+(?:my |the )?", "", site_text, flags=re.I)
    site_text = re.sub(r"\s+and\b.*", "", site_text).strip().rstrip(".")
    if not site_text:
        return None

    url = _site_url(site_text)
    if not url:
        return None

    launch_app(browser)
    time.sleep(0.7)
    webbrowser.open(url)
    SESSION.note_url(url)
    SESSION.note_app(browser)

    label = site_text.split()[0] if site_text else "the site"
    vision_goal = command.strip() if _needs_web_followthrough(text) else None
    return ToolResult(
        True,
        f"Opened {label} in {browser}.",
        vision_goal,
    )


def try_fast_tool(command: str) -> ToolResult | None:
    text = command.strip().lower()
    if not text:
        return None

    smart = try_smart_intent(command)
    if smart:
        return smart

    browser_open = try_open_in_browser(command)
    if browser_open:
        return browser_open

    open_match = re.search(
        r"\b(?:open|launch|start|run|go\s+to)\s+(?:my\s+|the\s+)?(.+?)"
        r"(?:\s+please|\s+for me|\s+and\b|\s+then\b|\.|$)",
        text,
    )
    if not open_match:
        return None

    target = _strip_browser_phrases(open_match.group(1).strip().rstrip("."))
    if not target:
        return None

    # Don't treat screen-referential phrases as an app/site to open.
    if re.search(
        r"\b(link|whichever|which ?ever|right one|correct one|result|option|"
        r"that you see|on screen|on the screen)\b",
        target,
    ):
        return None

    # An app/site name is short; long phrases are sentences, not targets.
    if len(target.split()) > 5:
        return None

    if target.startswith("http://") or target.startswith("https://"):
        return open_url(target)

    if "." in target and " " not in target:
        return open_url(f"https://{target}")

    if target in {"youtube", "youtube.com"}:
        return open_url("https://www.youtube.com", "Opened YouTube.")

    yt = parse_youtube_intent(command)
    if yt:
        return yt

    return _launch_app(target)


def open_url(url: str, message: str | None = None) -> ToolResult:
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    webbrowser.open(url)
    SESSION.note_url(url)
    return ToolResult(True, message or f"Opened {url}")


def google_search(query: str) -> ToolResult:
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    webbrowser.open(url)
    SESSION.note_url(url)
    SESSION.note_intent(f"searched: {query}")
    return ToolResult(True, f"Searched Google for '{query}'.")


def launch_app(name: str) -> ToolResult:
    return _launch_app(name)


def wait_seconds(seconds: float) -> ToolResult:
    time.sleep(max(0.0, seconds))
    return ToolResult(True, f"Waited {seconds}s")


def parse_youtube_intent(command: str) -> ToolResult | None:
    """Understand natural YouTube requests instead of searching the whole sentence."""
    text = command.strip().lower()
    if "youtube" not in text and "yt " not in text:
        return None

    wants_latest = bool(re.search(r"\b(latest|newest|most recent)\b", text))
    wants_play = bool(re.search(r"\b(show me|play|watch|find|open)\b", text))

    query: str | None = None
    patterns = [
        r"(?:show me|find|play|watch)\s+(?:me\s+)?(.+?)\s+(?:latest|newest|most recent)\s+video",
        r"(?:latest|newest|most recent)\s+video\s+(?:from|by|of)\s+(.+?)(?:\.|$)",
        r"(?:show me|find|play|watch)\s+(?:me\s+)?(.+?)\s+on\s+youtube",
        r"(?:search|look up)\s+(.+?)\s+on\s+youtube",
        r"youtube\s+(?:for\s+)?(.+?)(?:\.|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            query = _clean_youtube_query(match.group(1))
            if query:
                break

    if not query:
        query = _extract_youtube_search(text)

    if not query:
        if re.search(r"\b(open|launch)\s+(?:youtube|yt)\b", text):
            webbrowser.open("https://www.youtube.com")
            SESSION.note_url("https://www.youtube.com")
            return ToolResult(True, "Opened YouTube.")
        return None

    sort = "&sp=CAI%253D" if wants_latest else ""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}{sort}"
    webbrowser.open(url)
    SESSION.note_url(url)
    SESSION.note_intent(f"youtube: {query}")

    msg = f"Searching YouTube for '{query}'"
    if wants_latest:
        msg += " (newest first)"

    vision_goal = None
    if wants_latest or wants_play:
        vision_goal = (
            f"On the YouTube search results for '{query}', click the first / newest "
            f"video result that matches. Then return done."
        )

    return ToolResult(True, msg + ".", vision_goal)


def _clean_youtube_query(raw: str) -> str | None:
    q = raw.strip().lower().rstrip(".?!,")
    q = re.sub(r"^(?:please\s+)?(?:open|launch|go to)\s+", "", q)
    q = re.sub(r"\s+(?:on|in)\s+(?:google\s+)?chrome.*$", "", q)
    q = re.sub(r"\s+(?:on|in)\s+youtube.*$", "", q)
    q = re.sub(r"^(?:me\s+the\s+|the\s+|a\s+)", "", q)
    stop = {
        "youtube",
        "video",
        "latest",
        "newest",
        "chrome",
        "browser",
        "google",
        "for me",
        "please",
    }
    if not q or q in stop or len(q) < 2:
        return None
    return q


def _extract_youtube_search(text: str) -> str | None:
    if "youtube" not in text and "yt " not in text:
        return None
    patterns = [
        r"(?:search(?:\s+for)?|play|find|look\s+up)\s+(.+?)\s+(?:on|in)\s+youtube",
        r"play\s+(.+?)\s+on\s+youtube",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            query = _clean_youtube_query(match.group(1))
            if query:
                return query
    return None


def _extract_google_search(text: str) -> str | None:
    if _is_ui_control_command(text):
        return None
    patterns = [
        r"\b(?:search(?:\s+for|\s+)?|google(?:\s+for|\s+)?|look\s+up)\s+(.+?)(?:\s+on google|\s+in google|$)",
        r"\bfind\s+(.+?)(?:\s+online|\s+on google|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            query = match.group(1).strip()
            if query and len(query) > 2:
                return query
    return None


def _is_ui_control_command(text: str) -> bool:
    # "chrome" alone is not a UI-control verb — users say "open youtube in chrome".
    return bool(
        re.search(
            r"\b(close|closing|shut|click|press|type|switch|minimize|maximize|scroll|tab|tabs|window)\b",
            text,
        )
    )


def _launch_app(name: str) -> ToolResult:
    name = _strip_browser_phrases(name.strip())
    if not name:
        return ToolResult(False, "No app name given.")

    url = _site_url(name)
    if url:
        webbrowser.open(url)
        SESSION.note_url(url)
        return ToolResult(True, f"Opened {name} in your browser.")

    key = normalize_app_name(name)
    aliases = APP_ALIASES.get(key, [key])

    for candidate in aliases:
        if candidate.endswith(":"):
            try:
                subprocess.Popen(["cmd", "/c", "start", "", candidate], shell=False)
                SESSION.note_app(name)
                return ToolResult(True, f"Opened {name}.")
            except OSError as exc:
                return ToolResult(False, str(exc))

        if candidate in {"notepad", "calc", "explorer", "msedge", "chrome", "firefox"}:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", candidate], shell=False)
                SESSION.note_app(name)
                return ToolResult(True, f"Opened {name}.")
            except OSError:
                continue

    ok, message, _path = launch_application(name)
    if ok:
        SESSION.note_app(name)
        return ToolResult(ok, message)

    searched_ok, search_message = open_via_windows_search(name)
    if searched_ok:
        SESSION.note_app(name)
    return ToolResult(searched_ok, search_message)
