"""Detect fake progress: premature 'done', distractor clicks, platform pickers.

All rules are task-type based (install vs not), never tied to specific apps or games.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from screeny.ui_grounding import UIElement, best_match

_INSTALL_GOAL = re.compile(
    r"\b(install|download|set up|setup)\b",
    re.I,
)

_DISTRACTOR = re.compile(
    r"\b("
    r"gemini|ask gemini|copilot|chatgpt|bing chat|"
    r"sign in to chrome|customize chrome|"
    r"advertisement|sponsored|"
    r"newsletter|subscribe|cookie settings|"
    r"about this result|people also ask|related searches"
    r")\b",
    re.I,
)

_GOOD_INSTALL_CLICK = re.compile(
    r"\b("
    r"download|install|windows|win64|win 64|pc|"
    r"free download|get started|play for free|play free|play now|free to play|"
    r"next|finish|run|accept|agree|continue|"
    r"start|launch|open"
    r")\b",
    re.I,
)

_VENDOR_CTA = re.compile(
    r"\b(play for free|play free|play now|free to play|start playing|get the game)\b",
    re.I,
)

_VENDOR_NAV = re.compile(
    r"\b(official|home|menu|products|pricing|learn more|sign up|get)\b",
    re.I,
)


@dataclass
class TaskProgress:
    download_clicked: bool = False
    installer_launched: bool = False
    wizard_active: bool = False
    platform_selected: bool = False
    vendor_site_reached: bool = False
    off_track_clicks: int = 0
    no_change_clicks: int = 0

    def note_download_click(self) -> None:
        self.download_clicked = True

    def note_vendor_site(self) -> None:
        self.vendor_site_reached = True

    def note_installer_launched(self) -> None:
        self.installer_launched = True
        self.wizard_active = True

    def note_wizard(self) -> None:
        self.wizard_active = True


def goal_is_install_like(goal: str) -> bool:
    return bool(_INSTALL_GOAL.search(goal or ""))


def element_blob(elements: list[UIElement]) -> str:
    parts = [f"{e.kind}:{e.name}" for e in elements if e.name]
    return " ".join(parts).lower()


def platform_picker_visible(elements: list[UIElement]) -> bool:
    """True only for an actual OS chooser — not pages that merely mention PC/Mac."""
    if vendor_download_visible(elements):
        return False
    names = [e.name.strip() for e in elements if e.name.strip()]
    if not names:
        return False
    blob = " ".join(n.lower() for n in names)
    if re.search(r"\b(choose|select|pick)\b.*\bplatform\b", blob) or "which platform" in blob:
        return True
    tile_count = sum(1 for n in names if is_platform_tile(UIElement(n, "button", 0, 0, 0, 0)))
    return tile_count >= 2


def vendor_cta_visible(elements: list[UIElement]) -> bool:
    return bool(_VENDOR_CTA.search(element_blob(elements)))


def vendor_download_visible(elements: list[UIElement]) -> bool:
    return find_vendor_download_target(elements) is not None


def vendor_cta_hint(elements: list[UIElement]) -> str:
    if vendor_download_visible(elements):
        return (
            " NOTE: This page has a Download button in the main content — click "
            "DOWNLOAD / Download for Windows. Do NOT click Play Now in the top "
            "navigation bar (that opens sign-in, not the installer)."
        )
    if not vendor_cta_visible(elements):
        return ""
    return (
        " NOTE: If there is no Download button visible, Play for Free / Play Now "
        "may start the install path — but prefer an explicit Download button first."
    )


def _is_google_result_remnant(name: str) -> bool:
    low = name.lower()
    return len(name) > 55 or "http" in low or "›" in name or (
        "google" in low and "download" in low
    )


def is_platform_tile(element: UIElement | None) -> bool:
    if element is None or not element.name:
        return False
    return bool(
        re.fullmatch(r"(?i)(pc|mac|macos|windows)(\s+\1)?", element.name.strip())
    )


def is_vendor_header_cta(element: UIElement | None) -> bool:
    """Play Now / sign-in CTAs in the top nav bar — not the hero Download."""
    if element is None or not element.name:
        return False
    if not _VENDOR_CTA.search(element.name):
        return False
    return element.cy < 350


def find_vendor_download_target(elements: list[UIElement]) -> UIElement | None:
    """Prefer the hero/content Download button — not header Play Now or Google links."""
    candidates: list[tuple[UIElement, float]] = []
    for element in elements:
        if element.kind not in {"button", "link", "item"} or not element.name:
            continue
        name = element.name.strip()
        low = name.lower()
        if is_distractor_click(name, element) or _is_google_result_remnant(name):
            continue
        if re.search(r"\b(mac|ios|android|app store)\b", low):
            continue

        score = 0.0
        if re.fullmatch(r"download\.?", low):
            score = 100.0
        elif low in {"download the game", "download game", "free download"}:
            score = 85.0
        elif re.search(r"\bdownload\b", low):
            score = 55.0
            if "windows" in low or "for pc" in low:
                score += 15.0
        else:
            continue

        if element.cy > 350:
            score += 25.0
        elif element.cy < 200:
            score -= 20.0
        if element.kind == "button":
            score += 10.0
        candidates.append((element, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    best, best_score = candidates[0]
    return best if best_score >= 50.0 else None


def find_vendor_cta_target(elements: list[UIElement]) -> UIElement | None:
    for query in (
        "Play for Free",
        "Play Free",
        "Play Now",
        "Free to Play",
        "Get the Game",
        "Start Playing",
    ):
        hit = best_match(elements, query)
        if hit is not None:
            return hit
    for element in elements:
        if element.name and _VENDOR_CTA.search(element.name):
            return element
    return None


def check_install_hotkey(
    raw_keys: object,
    elements: list[UIElement],
    *,
    install_task: bool,
    thought: str = "",
    history: list[str] | None = None,
) -> tuple[str | None, list[str] | None]:
    """Return (error_message, remapped_keys). remapped_keys replaces keys when set."""
    if not install_task:
        return None, None
    from screeny.actions import hotkey_is_useless_for_install, parse_hotkey_keys

    keys = parse_hotkey_keys(raw_keys)
    if not keys:
        return None, None

    # Model often says "backspace" when it means browser Back — remap, don't block.
    if len(keys) == 1 and keys[0] == "backspace":
        if _thought_means_browser_back(thought):
            return None, ["alt", "left"]
        if _recently_typed(history or []):
            return None, None  # allow real backspace after typing

    if hotkey_is_useless_for_install(keys):
        key = keys[0]
        if key == "backspace":
            return (
                'Backspace only deletes text in a focused field — it does NOT go back in '
                "the browser. To go back use hotkey [\"alt\",\"left\"]. To fix text, click "
                "the field first, then backspace. To continue install, click Play for Free / Download.",
                None,
            )
        return (
            f'Hotkey "{key}" is not useful here. Use click for page buttons, or '
            'hotkey ["alt","left"] only if you are on the wrong website.',
            None,
        )

    if vendor_cta_visible(elements) and keys == ["alt", "left"]:
        return (
            "Do NOT go back — this page already shows Play for Free / Play Free / "
            "Download. Click that button to move toward the installer.",
            None,
        )
    return None, None


def _thought_means_browser_back(thought: str) -> bool:
    return bool(
        re.search(
            r"\b(go back|wrong (site|page|website)|previous page|navigate back|not the official|find the official)\b",
            thought or "",
            re.I,
        )
    )


def _recently_typed(history: list[str]) -> bool:
    recent = " ".join(history[-3:]).lower()
    return "typed" in recent or "type ->" in recent


def cookie_banner_hint(elements: list[UIElement]) -> str:
    blob = element_blob(elements)
    if not re.search(r"\bcookie|consent|privacy\b", blob):
        return ""
    if re.search(r"\baccept|agree|allow|got it|ok\b", blob):
        return (
            " NOTE: A cookie/consent banner may be blocking the page — click Accept / "
            "Agree / Allow all if you cannot reach Download yet."
        )
    return ""


def platform_picker_hint(elements: list[UIElement]) -> str:
    if not platform_picker_visible(elements):
        return ""
    return (
        " NOTE: A platform picker is visible — click Windows / PC / Download for Windows. "
        "Do NOT pick Mac. Do NOT click unrelated browser buttons (Gemini, Copilot, etc.)."
    )


def find_windows_platform_target(elements: list[UIElement]) -> UIElement | None:
    if not platform_picker_visible(elements):
        return None
    for query in (
        "Download for Windows",
        "Windows",
        "PC",
        "Windows 64-bit",
        "Download Windows",
    ):
        hit = best_match(elements, query)
        if hit is not None:
            return hit
    return None


def is_distractor_click(target: str, element: UIElement | None = None) -> bool:
    blob = f"{target} {element.name if element else ''}"
    if _DISTRACTOR.search(blob):
        return True
    if element and _DISTRACTOR.search(element.name):
        return True
    return False


_BROWSER_CHROME = re.compile(
    r"\b("
    r"address and search|downloads|google chrome|reload|forward|new tab|bookmark|"
    r"extensions|customize chrome|side panel|app menu|more actions|"
    r"search tabs|tab search|chrome menu"
    r")\b",
    re.I,
)


def is_browser_chrome_element(element: UIElement | None) -> bool:
    if element is None:
        return False
    if element.kind == "tab":
        return True
    name = element.name.lower()
    if element.kind == "textbox" and "address" in name:
        return True
    if name in {"extensions", "minimize", "maximize", "close"}:
        return True
    return bool(_BROWSER_CHROME.search(element.name))


def is_browser_download_click(target: str, element: UIElement | None) -> bool:
    """True when a click is trying to hit Download in the browser, not the installer."""
    blob = f"{target} {element.name if element else ''}".lower()
    if not re.search(r"\bdownload\b", blob):
        return False
    if element and element.kind == "link":
        return True
    if re.search(r"\b(download the game|download for windows|play for free)\b", blob):
        return True
    return bool(re.fullmatch(r"download\.?", blob.strip()) or blob.strip() == "download")


def looks_like_auth_wall(elements: list[UIElement]) -> bool:
    blob = element_blob(elements)
    if not re.search(
        r"\b(sign up|sign in|log in|create account|create an account|register)\b",
        blob,
        re.I,
    ):
        return False
    return find_vendor_download_target(elements) is None


def click_helps_install_goal(
    target: str,
    element: UIElement | None,
    goal: str,
    *,
    wizard_active: bool = False,
    installer_phase: bool = False,
    elements: list[UIElement] | None = None,
) -> bool:
    if not goal_is_install_like(goal):
        return True
    phase = wizard_active or installer_phase
    # Browser toolbar, tabs, and extensions are never install targets.
    if not installer_phase and is_browser_chrome_element(element):
        return False
    if phase and is_browser_chrome_element(element):
        return False
    if phase and element and element.kind == "textbox":
        return False
    blob = f"{target} {element.name if element else ''}"
    if phase and is_browser_download_click(target, element):
        return False
    if phase and re.search(r"\bdownload\b", blob, re.I) and not re.search(
        r"\b(install|next|finish|run)\b", blob, re.I
    ):
        return False
    if is_distractor_click(target, element):
        return False
    # On vendor download pages: hero Download beats header Play Now / platform tiles.
    if elements and element and vendor_download_visible(elements):
        if is_vendor_header_cta(element) or is_platform_tile(element):
            return False
        if _VENDOR_CTA.search(element.name) and not re.search(
            r"\bdownload\b", element.name, re.I
        ):
            return False
    if elements and looks_like_auth_wall(elements):
        if element and (
            _VENDOR_CTA.search(element.name)
            or is_platform_tile(element)
            or re.search(r"\b(sign up|sign in|create account)\b", element.name, re.I)
        ):
            return False
    if _GOOD_INSTALL_CLICK.search(blob):
        if elements and element and is_vendor_header_cta(element) and vendor_download_visible(elements):
            return False
        return True
    if _VENDOR_NAV.search(blob):
        return True
    return True


def distractor_click_message(
    target: str,
    element: UIElement | None = None,
    *,
    wizard_active: bool = False,
    installer_phase: bool = False,
    elements: list[UIElement] | None = None,
) -> str:
    name = element.name if element else target
    if wizard_active or installer_phase:
        return (
            f'Blocked click on "{name[:60]}" — the installer/setup window should be '
            "in front. Ignore the browser. Click Install / Next / Run / Finish on the "
            "setup window (large primary button, often center or bottom)."
        )
    if element and is_browser_chrome_element(element):
        return (
            f'Blocked click on "{name[:60]}" — that is browser chrome (toolbar, '
            "tab, extensions), not the page content. Click Download / Play for Free "
            "in the page body, or the official Google result link."
        )
    if elements and element and vendor_download_visible(elements):
        if is_vendor_header_cta(element) or is_platform_tile(element):
            return (
                f'Blocked click on "{name[:60]}" — use the large DOWNLOAD button '
                "in the page content (below Download the Game), not Play Now in "
                "the top bar or platform tiles."
            )
    if elements and looks_like_auth_wall(elements):
        return (
            "This is a sign-in page, not the installer. Use hotkey "
            '["alt","left"] to go back, then click the DOWNLOAD button on the '
            "vendor page — not Play Now in the header."
        )
    return (
        f'Blocked click on "{name[:60]}" — that is NOT part of the install '
        "(browser AI, ads, etc.). Use browser Back (hotkey alt+left) if needed, "
        "then click the vendor Download / Windows option."
    )


_WIZARD_BUTTON_RE = re.compile(
    r"^("
    r"install(\s+now)?|next|run|finish|get started|i agree|accept|continue"
    r")\.?$",
    re.I,
)

_WIZARD_BUTTON_REJECT = re.compile(
    r"\b(installed|youtube|games|connect|cancel|tools|all|library|store|"
    r"steam|epic|settings|help|close|back|forward|download)\b",
    re.I,
)


def _is_primary_wizard_button(name: str) -> bool:
    text = (name or "").strip()
    if not text or len(text) > 40:
        return False
    if _WIZARD_BUTTON_REJECT.search(text):
        return False
    return bool(_WIZARD_BUTTON_RE.match(text))


def find_wizard_button_target(elements: list[UIElement]) -> UIElement | None:
    """Strict match — only obvious setup wizard primary buttons."""
    for element in elements:
        if element.kind != "button" or not element.name:
            continue
        if _is_primary_wizard_button(element.name):
            return element
    return None


def reject_done(goal: str, progress: TaskProgress, history: list[str]) -> str | None:
    """Return a reason string if 'done' is too early; None if OK to finish."""
    if not goal_is_install_like(goal):
        return None

    hist = " ".join(history).lower()
    if re.search(r"\b(finished|completed|installed|all set|setup complete)\b", hist):
        if progress.installer_launched or progress.wizard_active:
            return None

    if progress.installer_launched or progress.wizard_active:
        if re.search(r"\b(finish|complete|installed|done)\b", hist):
            return None
        return (
            "Not done — an installer or setup window was opened but the wizard "
            "is not finished. Keep clicking Next / Install / Finish."
        )

    if progress.download_clicked:
        return (
            "Not done — a download may have started but the installer has not been "
            "run yet. Use open_download or open the .exe from Downloads, then "
            "complete the setup wizard."
        )

    return (
        "Not done — the software is NOT installed yet. Typical steps still needed: "
        "pick Windows if asked, click Download, wait for the file, run the "
        "installer, finish the wizard. Do NOT return done while still on a website."
    )


def post_click_feedback(
    goal: str,
    *,
    before_hash: str,
    after_hash: str,
    before_elements: list[UIElement],
    after_elements: list[UIElement],
) -> str | None:
    if before_hash == after_hash:
        return (
            "That click did nothing useful — the screen looks the same. "
            "Try a different button or scroll to find Download / Windows."
        )

    if not goal_is_install_like(goal):
        return None

    after_blob = element_blob(after_elements)
    if _DISTRACTOR.search(after_blob):
        return (
            "That click opened something unrelated (Gemini / AI sidebar / junk). "
            'Use hotkey ["alt","left"] to go back, then click Download for Windows.'
        )

    before_picker = platform_picker_visible(before_elements)
    after_picker = platform_picker_visible(after_elements)
    if before_picker and not after_picker and not _GOOD_INSTALL_CLICK.search(after_blob):
        if _DISTRACTOR.search(after_blob) or len(after_elements) < 3:
            return (
                "You may have left the platform/download page without choosing Windows. "
                "Go back and click the Windows / PC download option."
            )

    return None


def screen_looks_stuck(elements: list[UIElement], history: list[str]) -> str | None:
    """Detect long stretches with no useful controls."""
    if len(elements) >= 4:
        return None
    hist = " ".join(history[-6:]).lower()
    if hist.count("wait") >= 3 and len(elements) <= 2:
        return (
            "The screen looks empty or stuck after waiting. Try scrolling, "
            "browser back, or a different click — do not keep waiting."
        )
    return None
