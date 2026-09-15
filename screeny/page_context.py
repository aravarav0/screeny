"""Detect what kind of page is on screen (Google results vs vendor site)."""

from __future__ import annotations

import re

from screeny.context import SESSION
from screeny.progress_check import element_blob
from screeny.ui_grounding import UIElement


def last_url_is_google_search() -> bool:
    url = (SESSION.last_url or "").lower()
    return "google.com/search" in url or (
        "google." in url and "/search" in url
    )


def url_from_link_text(name: str) -> str | None:
    """Pull a site URL out of a UIA link name like 'VALORANT https://playvalorant.com › en-us'."""
    if not name:
        return None
    m = re.search(r"(https?://[^\s›]+)", name)
    if m:
        return m.group(1).rstrip("/")
    m = re.search(r"\b([a-z0-9][-a-z0-9]*\.(?:com|gg|net|io|org)(?:/[^\s›]*)?)", name, re.I)
    if m:
        host = m.group(1)
        return host if host.startswith("http") else f"https://{host}"
    return None


def looks_like_google_results(elements: list[UIElement]) -> bool:
    blob = element_blob(elements)
    if not blob:
        return last_url_is_google_search()
    if "google" in blob and re.search(r"\b(search|results|images|videos)\b", blob):
        return True
    return last_url_is_google_search()


_GOOGLE_JUNK = re.compile(
    r"\b(about this result|people also ask|related searches|feedback|"
    r"more places|report inappropriate|cached|translate this page)\b",
    re.I,
)


def find_official_search_result(
    elements: list[UIElement], command: str
) -> UIElement | None:
    """Pick the best official-looking Google result link for an install/search goal."""
    from screeny.install import extract_install_target

    subject = (extract_install_target(command) or "").strip().lower()
    if not subject:
        return None

    best: UIElement | None = None
    best_score = 0.0
    for el in elements:
        name = (el.name or "").strip()
        if not name or _GOOGLE_JUNK.search(name):
            continue
        low = name.lower()
        if el.kind not in {"link", "item"} and "http" not in low and "›" not in name:
            continue
        score = 0.0
        if subject in low:
            score += 12.0
        for word in subject.split():
            if len(word) > 2 and word in low:
                score += 3.0
        if re.search(r"\b(official|download)\b", low):
            score += 4.0
        if re.search(r"\.(com|gg|net|io)\b", low) or "›" in name:
            score += 3.0
        if "google" in low and subject not in low:
            score -= 8.0
        if score > best_score:
            best_score = score
            best = el
    return best if best_score >= 10.0 else None


def on_vendor_or_app_page(elements: list[UIElement]) -> bool:
    """True when we're past Google — on a real site or installer."""
    if looks_like_google_results(elements):
        return False
    if not elements:
        return not last_url_is_google_search()
    blob = element_blob(elements)
    if last_url_is_google_search():
        # SESSION.last_url is often still the Google SERP after clicking a result.
        if re.search(r"playvalorant|riotgames|\.(com|gg|net|io)\b", blob, re.I):
            return True
        if "google" not in blob and len(elements) >= 6:
            return True
        return False
    return True
