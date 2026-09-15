"""Explicit install flow phases — whitelist intents per phase."""

from __future__ import annotations

from enum import Enum

from screeny.page_context import looks_like_google_results, on_vendor_or_app_page
from screeny.progress_check import goal_is_install_like
from screeny.ui_grounding import UIElement


class InstallPhase(str, Enum):
    SEARCH = "search"
    SEARCH_RESULTS = "search_results"
    VENDOR_PAGE = "vendor_page"
    DOWNLOAD_WAIT = "download_wait"
    INSTALLER_OPEN = "installer_open"
    WIZARD = "wizard"
    INSTALLING = "installing"
    DONE = "done"
    STUCK = "stuck"


PHASE_LABELS = ("Search", "Download", "Install", "Setup")

ALLOWED_INTENTS: dict[InstallPhase, frozenset[str]] = {
    InstallPhase.SEARCH_RESULTS: frozenset({"open_official_result", "click", "wait"}),
    InstallPhase.VENDOR_PAGE: frozenset(
        {"click_download", "pick_windows", "scroll", "dismiss_cookie", "click", "wait"}
    ),
    InstallPhase.DOWNLOAD_WAIT: frozenset({"wait", "open_download"}),
    InstallPhase.INSTALLER_OPEN: frozenset({"open_download", "wait", "click"}),
    InstallPhase.WIZARD: frozenset({"click_wizard_button", "click", "wait", "hotkey"}),
    InstallPhase.INSTALLING: frozenset({"wait", "click", "done"}),
}


def phase_to_progress_index(phase: InstallPhase) -> int | None:
    mapping = {
        InstallPhase.SEARCH: 0,
        InstallPhase.SEARCH_RESULTS: 0,
        InstallPhase.VENDOR_PAGE: 1,
        InstallPhase.DOWNLOAD_WAIT: 1,
        InstallPhase.INSTALLER_OPEN: 2,
        InstallPhase.WIZARD: 3,
        InstallPhase.INSTALLING: 3,
        InstallPhase.DONE: 3,
    }
    return mapping.get(phase)


def detect_install_phase(
    *,
    goal: str,
    elements: list[UIElement],
    download_clicked: bool,
    installer_launched: bool,
    wizard_active: bool,
) -> InstallPhase | None:
    if not goal_is_install_like(goal):
        return None
    if wizard_active or installer_launched:
        return InstallPhase.WIZARD
    if download_clicked:
        return InstallPhase.DOWNLOAD_WAIT
    if on_vendor_or_app_page(elements):
        return InstallPhase.VENDOR_PAGE
    if looks_like_google_results(elements):
        return InstallPhase.SEARCH_RESULTS
    return InstallPhase.SEARCH
