from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from screeny.app_finder import find_application, normalize_app_name
from screeny.config import SETTINGS
from screeny.tools import ToolResult, google_search, launch_app, open_url, wait_seconds

# Direct Windows installer URLs (official sources).
OFFICIAL_INSTALLERS: dict[str, str] = {
    "spotify": "https://download.scdn.co/SpotifySetup.exe",
    "discord": "https://discord.com/api/downloads/dist/app/win?platform=win&arch=x86",
    "chrome": "https://dl.google.com/chrome/install/latest/chrome_installer.exe",
    "firefox": "https://download.mozilla.org/?product=firefox-latest&os=win64&lang=en-US",
    "steam": "https://cdn.akamai.steamstatic.com/client/installer/SteamSetup.exe",
    "zoom": "https://zoom.us/client/latest/ZoomInstallerFull.exe",
    "slack": "https://slack.com/ssb/downloadWin64",
    "teams": "https://go.microsoft.com/fwlink/?linkid=2196101",
    "vscode": "https://code.visualstudio.com/sha/download?build=stable&os=win32-x64-user",
    "vlc": "https://get.videolan.org/vlc/last/win64/vlc-win64.exe",
    "7zip": "https://www.7-zip.org/a/7z2409-x64.exe",
    "notion": "https://www.notion.so/desktop/windows/download",
    "telegram": "https://telegram.org/dl/desktop/win64_portable",
}

OFFICIAL_DOWNLOAD_PAGES: dict[str, str] = {
    "spotify": "https://www.spotify.com/download/windows/",
    "discord": "https://discord.com/download",
}

INSTALL_ALIASES: dict[str, str] = {
    "google chrome": "chrome",
    "microsoft teams": "teams",
    "visual studio code": "vscode",
    "vs code": "vscode",
    "7-zip": "7zip",
    "epic launcher": "epic games launcher",
}

MAX_SIMPLE_INSTALL_TARGET_LEN = 40


def try_install(command: str) -> ToolResult | None:
    text = command.strip().lower()
    if not _looks_like_install(text):
        return None

    app = extract_install_target(command)
    if not app:
        return None
    return install_app(app)


def extract_install_target(command: str) -> str | None:
    text = command.strip().lower()
    if not _looks_like_install(text):
        return None

    patterns = (
        r"(?:download|install|get|set up|setup)\s+(?:the\s+)?(?:launcher\s+for\s+)?(.+?)(?:\s+from\b|\s+using\b|\s+via\b|\s+in\b|\s+on\b|\s+with\b|\s+if\b|\s+please|\s+for me|$)",
        r"(?:don'?t|do not)\s+have\s+(?:the\s+)?(.+?)(?:\s*,|\s+please|\s+can you|\s+download|\s+install|$)",
        r"(?:need|want)\s+(?:to download|to install|)\s*(?:the\s+)?(?:launcher\s+for\s+)?(.+?)(?:\s+from\b|\s+app|\s+please|$)",
    )

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            target = _clean_target(match.group(1))
            if target and len(target) <= MAX_SIMPLE_INSTALL_TARGET_LEN:
                return target

    return None


def install_app(app_name: str) -> ToolResult:
    name = _resolve_install_name(app_name)
    if not name:
        return ToolResult(False, "I couldn't tell which app to install.")

    display = _display_name(name)

    if find_application(name):
        launch = launch_app(name)
        if launch.ok:
            return ToolResult(
                True,
                f"{display} is already installed. I opened it for you.",
            )
        return ToolResult(True, f"{display} looks installed already.")

    direct_url = _lookup(OFFICIAL_INSTALLERS, name)
    if direct_url:
        try:
            installer = download_installer(direct_url, name)
            run_installer(installer)
            return ToolResult(
                True,
                f"Downloaded {display} and started the installer.",
                _installer_vision_goal(display),
            )
        except Exception as exc:
            page = _lookup(OFFICIAL_DOWNLOAD_PAGES, name)
            if page:
                open_url(page)
                wait_seconds(2.0)
                return ToolResult(
                    True,
                    f"Opened the official {display} download page.",
                    _browser_install_goal(display),
                )
            return ToolResult(False, f"Download failed for {display}: {exc}")

    page = _lookup(OFFICIAL_DOWNLOAD_PAGES, name)
    if page:
        open_url(page)
        wait_seconds(2.0)
        return ToolResult(
            True,
            f"Opened the official {display} download page.",
            _browser_install_goal(display),
        )

    google_search(f"{display} download official site")
    wait_seconds(2.0)
    return ToolResult(
        True,
        f"Searched Google for the official {display} download.",
        _browser_install_goal(display),
    )


def download_installer(url: str, app_name: str) -> Path:
    SETTINGS.download_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True, timeout=180.0) as client:
        response = client.get(url)
        response.raise_for_status()
        filename = _filename_from_response(response, app_name, url)
        path = SETTINGS.download_dir / filename
        path.write_bytes(response.content)

    if path.stat().st_size < 1024:
        raise RuntimeError("Downloaded file looks too small to be a valid installer.")

    return path


def run_installer(path: Path) -> None:
    os.startfile(path)  # noqa: S606


def is_complex_install(command: str) -> bool:
    """Installs that need multi-phase LLM planning (unknown apps, long requests)."""
    text = command.strip().lower()
    if len(text) > 100:
        return True
    if text.count("download") > 1:
        return True
    if re.search(r"\b(and then|then once|first .+ then|once .+ downloaded)\b", text):
        return True
    if re.search(
        r"\bfrom (?:google )?chrome\b|\bin (?:the )?browser\b|\busing chrome\b|\bthrough chrome\b",
        text,
    ):
        return True
    if not _looks_like_install(text):
        return False
    # Short "install X" uses the generic template plan + vision — no extra planner LLM.
    return False


def _should_use_planner_for_install(text: str) -> bool:
    return is_complex_install(text)


def _looks_like_install(text: str) -> bool:
    return bool(
        re.search(
            r"\b(download|install|set up|setup|get me|get the)\b",
            text,
        )
        or re.search(r"\b(don'?t|do not)\s+have\b", text)
    )


def _resolve_install_name(app_name: str) -> str:
    name = normalize_app_name(app_name)
    if not name:
        return ""
    return INSTALL_ALIASES.get(name, name)


def _lookup(mapping: dict[str, str], name: str) -> str | None:
    if name in mapping:
        return mapping[name]
    alias = INSTALL_ALIASES.get(name)
    if alias and alias in mapping:
        return mapping[alias]
    return None


def _clean_target(text: str) -> str:
    text = normalize_app_name(text)
    text = re.sub(r"^(me|the|a|an)\s+", "", text)
    text = re.sub(r"\b(app|application|program|software|installer|launcher)\b", "", text).strip()
    text = re.sub(r"\b(it|one|that|this)\b", "", text).strip()
    # Generic nouns that mean "download X" never actually named an app.
    text = re.sub(r"\b(site|website|page|link|file|here|online|version|now)\b", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _display_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split())


def _filename_from_response(response: httpx.Response, app_name: str, url: str) -> str:
    cd = response.headers.get("content-disposition", "")
    if "filename=" in cd:
        raw = cd.split("filename=", 1)[1].strip().strip('"')
        return _safe_filename(unquote(raw))

    parsed = urlparse(str(response.url))
    name = Path(unquote(parsed.path)).name
    if name and "." in name:
        return _safe_filename(name)

    return _safe_filename(f"{app_name.replace(' ', '_')}_setup.exe")


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "installer.exe"


def _installer_vision_goal(app_name: str) -> str:
    return (
        f"A setup wizard for {app_name} may be open. Click Next, accept defaults, "
        f"Install, and Finish to complete the {app_name} installation."
    )


def _browser_install_goal(app_name: str) -> str:
    return (
        f"Goal: download and install {app_name} on Windows. Continue from whatever is "
        f"on screen now (Google results, a vendor site, a platform picker, or a download page). "
        f"Step by step: (1) If on search results, click the official vendor link — not ads or "
        f"mirrors. (2) Dismiss cookie/consent banners if they block the page (Accept / Agree). "
        f"(3) If asked Windows vs Mac, choose Windows / PC. "
        f"(4) Click Download, Play for Free, Play Free, or Get — once. "
        f"(5) WAIT for the .exe/.msi to finish downloading, then use open_download or open the file. "
        f"(6) Work through the setup wizard: Yes/Run/Next/Install/Finish. "
        f"Do NOT return done until the installer has run or the app is installed. "
        f"If UAC/SmartScreen blocks you, use ask for the user."
    )
