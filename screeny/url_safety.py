"""Block planner-hallucinated URLs; prefer Google search for unknown installs."""

from __future__ import annotations

from urllib.parse import urlparse

from screeny.install import (
    OFFICIAL_DOWNLOAD_PAGES,
    OFFICIAL_INSTALLERS,
    _looks_like_install,
    _lookup,
    _resolve_install_name,
    extract_install_target,
)
from screeny.tools import SMART_URLS, ToolResult, google_search, open_url

_TRUSTED: set[str] = {
    "google.com",
    "youtube.com",
    "youtu.be",
    "github.com",
    "microsoft.com",
    "apple.com",
    "spotify.com",
    "discord.com",
    "mozilla.org",
    "videolan.org",
    "zoom.us",
    "slack.com",
    "notion.so",
    "telegram.org",
    "7-zip.org",
    "visualstudio.com",
    "aka.ms",
    "steampowered.com",
    "steamstatic.com",
    "netflix.com",
    "x.com",
    "twitter.com",
}

for _url, _msg, _vg in SMART_URLS.values():
    host = urlparse(_url).netloc.lower().removeprefix("www.")
    if host:
        _TRUSTED.add(host)


def _host_trusted(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return False
    if not host:
        return False
    return any(host == root or host.endswith(f".{root}") for root in _TRUSTED)


def _google_fallback_query(command: str, url: str = "") -> str:
    target = extract_install_target(command)
    if target:
        return f"{target} download official site"
    if url:
        host = urlparse(url).netloc.replace("www.", "")
        if host:
            return f"{host} official download"
    text = command.strip()[:60]
    return f"{text} official site" if text else "official download site"


def planner_open_url(url: str, command: str) -> ToolResult:
    """Open a URL from the planner, or search Google if the URL is not trusted."""
    url = (url or "").strip()
    if not url:
        return ToolResult(False, "No URL provided.")

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    if _looks_like_install(command.lower()):
        query = _google_fallback_query(command, url)
        return google_search(query)

    if _host_trusted(url):
        return open_url(url)

    query = _google_fallback_query(command, url)
    print(f"  url blocked (untrusted): {url} -> google: {query}")
    return google_search(query)


def _catalog_has_app(app_name: str) -> bool:
    name = _resolve_install_name(app_name)
    if not name:
        return False
    return bool(
        _lookup(OFFICIAL_INSTALLERS, name) or _lookup(OFFICIAL_DOWNLOAD_PAGES, name)
    )


def compact_planner_steps(steps: list, command: str) -> list:
    """One search, one short wait, drop bogus install_app — then vision."""
    steps = sanitize_planner_steps(steps, command)
    if not isinstance(steps, list):
        return steps

    cleaned: list = []
    did_search = False
    did_wait = False

    for step in steps:
        if not isinstance(step, dict):
            cleaned.append(step)
            continue

        action = str(step.get("action", "")).lower().strip()

        if action == "install_app":
            app = str(step.get("app", "")).strip()
            if _catalog_has_app(app):
                cleaned.append(step)
            elif not did_search:
                cleaned.append(
                    {
                        "action": "google_search",
                        "query": _google_fallback_query(command),
                    }
                )
                did_search = True
            continue

        if action in {"google_search", "open_url"}:
            if did_search:
                continue
            if action == "open_url":
                cleaned.append(
                    {
                        "action": "google_search",
                        "query": _google_fallback_query(
                            command, str(step.get("url", ""))
                        ),
                    }
                )
            else:
                cleaned.append(step)
            did_search = True
            continue

        if action == "wait":
            if did_wait:
                continue
            seconds = step.get("seconds", 2)
            try:
                seconds = min(float(seconds), 2.0)
            except (TypeError, ValueError):
                seconds = 2.0
            cleaned.append({"action": "wait", "seconds": seconds})
            did_wait = True
            continue

        cleaned.append(step)

    return cleaned


def sanitize_planner_steps(steps: list, command: str) -> list:
    """Replace risky open_url steps before execution."""
    if not isinstance(steps, list):
        return steps

    install = _looks_like_install(command.lower())
    cleaned: list = []

    for step in steps:
        if not isinstance(step, dict):
            cleaned.append(step)
            continue
        action = str(step.get("action", "")).lower().strip()
        if action == "open_url" and install:
            query = _google_fallback_query(command, str(step.get("url", "")))
            cleaned.append({"action": "google_search", "query": query})
            continue
        if action == "open_url":
            url = str(step.get("url", "")).strip()
            full = url if url.startswith("http") else f"https://{url}"
            if url and not _host_trusted(full):
                query = _google_fallback_query(command, url)
                cleaned.append({"action": "google_search", "query": query})
                continue
        cleaned.append(step)

    return cleaned
