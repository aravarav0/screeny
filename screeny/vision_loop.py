from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from screeny.actions import (
    ActionResult,
    CoordError,
    download_in_progress,
    execute,
    find_recent_installer,
    normalize_action_name,
    open_installer,
    resolve_coords,
)
from screeny.config import SETTINGS
from screeny.context import SESSION
from screeny.events import NO_OP_EVENTS, AgentEvents
from screeny.ollama_client import OllamaError, chat_json
from screeny.prompts import (
    TEXT_ONLY_SYSTEM,
    TEXT_ONLY_USER,
    VISION_REFINE_SYSTEM,
    VISION_REFINE_USER,
    VISION_SYSTEM,
    VISION_USER_TEMPLATE,
)
from screeny.progress_check import (
    TaskProgress,
    check_install_hotkey,
    click_helps_install_goal,
    cookie_banner_hint,
    distractor_click_message,
    find_vendor_cta_target,
    find_vendor_download_target,
    find_windows_platform_target,
    find_wizard_button_target,
    filter_vendor_candidates,
    goal_is_install_like,
    is_browser_download_click,
    is_distractor_click,
    is_platform_tile,
    is_vendor_header_cta,
    looks_like_auth_wall,
    platform_picker_hint,
    platform_picker_visible,
    post_click_feedback,
    reject_done,
    screen_looks_stuck,
    vendor_cta_hint,
    vendor_download_visible,
)
from screeny.installer_window import get_foreground_window_rect
from screeny.page_context import (
    find_official_search_result,
    looks_like_google_results,
    on_vendor_or_app_page,
    url_from_link_text,
)
from screeny.screen import Capture, capture_primary_monitor
from screeny.session_log import log as session_log
from screeny.install_phases import InstallPhase, detect_install_phase, phase_to_progress_index
from screeny.ui_grounding import (
    UIElement,
    best_match,
    browser_viewport_rect,
    click_point_for_element,
    filter_elements_for_browser,
    format_elements,
    get_clickable_elements,
    in_viewport,
    interact_element,
    is_taskbar_zone,
    page_signature,
    pick_by_label,
    wait_for_page_settle,
)
from screeny.vendor_download_locator import locate_vendor_download, ocr_viewport

SpeakFn = Callable[[str], None]

ESCALATION: dict[str, str] = {
    "uia": "ocr",
    "ocr": "scroll",
    "scroll": "grid",
    "grid": "vision",
    "vision": "stuck",
}


@dataclass
class EscalationState:
    tier: str = "uia"
    last_key: tuple[str, str, int, int] | None = None
    repeats: int = 0
    banned_targets: set[str] = field(default_factory=set)


def _action_key_tier(action: str, tier: str, x: int, y: int) -> tuple[str, str, int, int]:
    return (action, tier, x // 40, y // 40)


def register_outcome(
    state: EscalationState,
    key: tuple[str, str, int, int],
    *,
    blocked: bool,
    target_name: str = "",
) -> str:
    if blocked and target_name:
        state.banned_targets.add(target_name.strip().lower())
    if blocked or key == state.last_key:
        state.repeats += 1
    else:
        state.last_key, state.repeats = key, 1
    if state.repeats >= 2:
        state.tier = ESCALATION.get(state.tier, "stuck")
        state.repeats = 0
        session_log(f"escalate tier -> {state.tier}")
    return state.tier


@dataclass
class VisionRunResult:
    ok: bool
    steps: int
    message: str
    cancelled: bool = False


def _auto_open_download(
    since: float,
    events: AgentEvents,
    stop_event: threading.Event | None,
    cancel_event: threading.Event | None,
    opened: set[str],
    *,
    timeout: float = 35.0,
) -> ActionResult | None:
    """Deterministically run the installer that was just downloaded.

    Polls the Downloads folder for a completed .exe/.msi (newer than `since`)
    and launches it — instead of trusting the model to do it. Waits while a
    partial-download file is still being written. Returns the launch result, or
    None if nothing showed up (e.g. the click didn't actually start a download).
    """
    deadline = time.time() + timeout
    announced = False
    while time.time() < deadline:
        if _should_stop(stop_event, cancel_event):
            return None
        target = find_recent_installer(since)
        if target is not None:
            result = open_installer(target, opened=opened)
            if result.ok and "already opened" not in result.detail:
                time.sleep(SETTINGS.installer_open_wait)
            return result
        if download_in_progress(since):
            if not announced:
                events.on_status("Download in progress — I'll run the installer when it's ready")
                announced = True
            time.sleep(1.0)
            continue
        # Nothing downloading and nothing arrived yet. Give it a short grace
        # period after the click (the download may take a beat to register),
        # then conclude the click didn't produce a file.
        if time.time() - since > 8.0:
            return None
        time.sleep(1.0)
    return None


def _download_wait_loop(
    *,
    download_click_ts: float,
    events: AgentEvents,
    stop_event: threading.Event | None,
    cancel_event: threading.Event | None,
    opened_installers: set[str],
    progress: TaskProgress,
    timeout: float = 90.0,
) -> tuple[str, bool, bool]:
    """Pure polling. No element scan, no LLM, no browser clicks except auto-open."""
    deadline = download_click_ts + timeout
    while time.time() < deadline:
        if _should_stop(stop_event, cancel_event):
            return "Stopped.", False, False
        opened = _auto_open_download(
            download_click_ts,
            events,
            stop_event,
            cancel_event,
            opened_installers,
            timeout=5.0,
        )
        if opened is not None and opened.ok:
            progress.note_installer_launched()
            return _wizard_open_message(opened.detail), True, True
        if download_in_progress(download_click_ts):
            deadline = time.time() + timeout
        time.sleep(2.0)
    return "", False, False


def _save_ocr_miss_debug(
    capture: Capture,
    viewport: tuple[int, int, int, int],
    *,
    step: int,
    used_fallback_vp: bool,
) -> None:
    """Save viewport crop and log OCR words when DOWNLOAD is not found."""
    left, top, right, bottom = viewport
    debug_dir = SETTINGS.screenshot_dir.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        from screeny.screen import crop_native_image

        crop_img, _, _, _, _ = crop_native_image(capture, left, top, right, bottom)
        path = debug_dir / f"ocr_miss_step{step}.png"
        crop_img.save(path)
    except Exception as exc:
        session_log(f"ocr miss debug save failed: {exc}")
        return
    detections = ocr_viewport(capture, viewport)
    top_words = sorted(detections, key=lambda d: d[2], reverse=True)[:10]
    words = ", ".join(f"{t!r}:{c:.2f}" for _, t, c in top_words)
    session_log(
        f"ocr miss step {step}: vp={viewport} native={capture.native_width}x{capture.native_height} "
        f"fallback_vp={used_fallback_vp} top10=[{words}] saved={path.name}"
    )


def _click_vendor_download_via_ocr(
    capture: Capture,
    *,
    allow_scroll: bool = True,
    step: int = 0,
) -> ActionResult | None:
    """OCR-first DOWNLOAD with viewport crop, scoring, and optional scroll."""
    vp = browser_viewport_rect(capture.native_width, capture.native_height)
    used_fallback_vp = vp is None
    if vp is None:
        rect = get_foreground_window_rect()
        if rect is None:
            return None
        vp = rect
        session_log(f"ocr viewport fallback: foreground window {vp}")

    hit = locate_vendor_download(capture, vp)
    if hit is None and allow_scroll:
        from screeny.vendor_download_locator import MAX_SCROLLS

        for _ in range(MAX_SCROLLS):
            execute(
                {"action": "scroll", "amount": -600},
                screen_width=capture.native_width,
                screen_height=capture.native_height,
            )
            time.sleep(SETTINGS.page_load_wait)
            try:
                capture = capture_primary_monitor()
            except Exception:
                break
            vp = browser_viewport_rect(capture.native_width, capture.native_height) or vp
            hit = locate_vendor_download(capture, vp)
            if hit is not None:
                break

    if hit is None:
        from screeny.window_pointer import find_in_window

        taskbar_y = int(capture.native_height * 0.88)
        best_pt = None
        for query in ("DOWNLOAD", "Download"):
            pt = find_in_window(query, capture, vp)
            if pt is None or "download" not in pt.label.lower():
                continue
            if pt.y >= taskbar_y:
                continue
            if best_pt is None or pt.y > best_pt.y:
                best_pt = pt
        if best_pt is None:
            if step:
                _save_ocr_miss_debug(
                    capture, vp, step=step, used_fallback_vp=used_fallback_vp
                )
            return None
        hit_x, hit_y, hit_label = best_pt.x, best_pt.y, best_pt.label
    else:
        hit_x, hit_y, hit_label = hit.x, hit.y, hit.text

    if not in_viewport(hit_x, hit_y, vp):
        return None

    try:
        return execute(
            {
                "action": "click",
                "x": hit_x,
                "y": hit_y,
                "_native": True,
                "_hybrid": True,
            },
            screen_width=capture.native_width,
            screen_height=capture.native_height,
            image_width=capture.native_width,
            image_height=capture.native_height,
        )
    except Exception:
        return None


def _after_vendor_download_click(
    *,
    step: int,
    detail: str,
    label: str,
    progress: TaskProgress,
    events: AgentEvents,
    history: list[str],
) -> tuple[str, float]:
    """Record a confirmed vendor download click; polling happens in DOWNLOAD_WAIT."""
    progress.note_vendor_site()
    progress.note_download_click()
    ts = time.time()
    last_result = f"Clicked {label} — waiting for installer download."
    history.append(f"step {step}: vendor download -> {detail}")
    session_log(f"vision step {step} vendor download: {label[:60]}")
    session_log(f"vision step {step} phase -> DOWNLOAD_WAIT (LLM locked)")
    events.on_action(detail)
    return last_result, ts


def _is_vendor_download_click(
    target: str,
    element: UIElement | None,
    *,
    on_vendor_page: bool,
    viewport: tuple[int, int, int, int] | None,
    click_y: int,
) -> bool:
    """Download intent counts ONLY on a vendor page, in the content area."""
    if not on_vendor_page or viewport is None:
        return False
    left, top, right, bottom = viewport
    vh = max(1, bottom - top)
    if click_y < top + 0.18 * vh or click_y > bottom:
        return False
    blob = f"{target} {element.name if element else ''}".lower()
    if re.search(
        r"\b(play|launch|sign[ -]?in|log[ -]?in|mac|ios|android|app store)\b",
        blob,
    ):
        return False
    return bool(re.search(r"\b(download|install now|get for windows)\b", blob))


_WIZARD_KEYWORDS = (
    "install",
    "next",
    "finish",
    "done",
    "accept",
    "agree",
    "continue",
    "run",
    "start",
    "confirm",
    "yes",
    "ok",
    "close",
    "get started",
    "i agree",
)


def _click_target(decision: dict) -> str:
    """Short description of what to click — used for UI-TARS and refine."""
    target = str(decision.get("target") or "").strip()
    if target:
        return target
    thought = str(decision.get("thought") or "").strip().lower()
    for kw in _WIZARD_KEYWORDS:
        if kw in thought:
            return f"{kw} button"
    return str(decision.get("thought") or "the target").strip()[:80]


def _is_wizard_target(target: str) -> bool:
    low = target.lower()
    return any(kw in low for kw in _WIZARD_KEYWORDS)


def _uia_match_trustworthy(element: UIElement, target: str) -> bool:
    """True when a UIA element plausibly matches what we're trying to click."""
    if not _is_wizard_target(target):
        return True
    name = element.name.lower()
    if not name:
        return False
    low = target.lower()
    return any(kw in name for kw in _WIZARD_KEYWORDS if kw in low) or any(
        kw in name for kw in _WIZARD_KEYWORDS
    )


def _likely_custom_ui(elements: list[UIElement]) -> bool:
    """Game/custom installers often expose almost nothing via accessibility."""
    if len(elements) <= 2:
        return True
    useful = [e for e in elements if e.kind in {"button", "link", "checkbox"} and e.name]
    return len(useful) == 0


def _wizard_open_message(detail: str) -> str:
    return (
        f"{detail}. A setup/installer window should now be open — look for large "
        "primary buttons (Install, Next, Run, Finish) and click them one step at a "
        "time. These custom-drawn buttons are NOT in the element list — describe "
        "the button in 'target' and point at its center. Do NOT click Download again."
    )


def _wizard_goal(original: str) -> str:
    return (
        "The installer/setup window is open. IGNORE the web browser entirely. "
        "Click through the setup wizard: Install, then Next, then Finish (accept "
        f"defaults). Original task: {original[:120]}"
    )


def _in_installer_phase(
    progress: TaskProgress,
    setup_wizard_active: bool,
    opened_installers: set[str],
) -> bool:
    return (
        setup_wizard_active
        or progress.installer_launched
        or bool(opened_installers)
    )


def _try_focus_installer(
    progress: TaskProgress,
    *,
    after_download: bool = False,
) -> str | None:
    """Focus installer window; use lower bar right after a browser download."""
    from screeny.installer_window import MIN_SETUP_SCORE, focus_setup_window_if_present

    min_score = 10 if after_download else MIN_SETUP_SCORE
    title = focus_setup_window_if_present(min_score=min_score)
    if title:
        progress.note_installer_launched()
    return title


def run_vision_task(
    goal: str,
    *,
    max_steps: int | None = None,
    speak: SpeakFn | None = None,
    stop_event: threading.Event | None = None,
    cancel_event: threading.Event | None = None,
    events: AgentEvents | None = None,
) -> VisionRunResult:
    events = events or NO_OP_EVENTS
    limit = max_steps or SETTINGS.max_vision_steps
    last_result = "starting"
    SETTINGS.screenshot_dir.mkdir(parents=True, exist_ok=True)

    history: list[str] = []
    recent_actions: list[str] = []
    last_click_key: str | None = None
    last_click_was_download = False
    download_click_ts = 0.0
    opened_installers: set[str] = set()
    setup_wizard_active = False
    consecutive_waits = 0
    consecutive_errors = 0
    unchanged_steps = 0
    last_screen_hash: str | None = None
    last_was_wait = False
    progress = TaskProgress()
    escalate_state = EscalationState()
    task_started_at = time.time()
    install_task = goal_is_install_like(goal)

    session_context = SESSION.context_for_agent(goal) or "(first request this session)"
    settings_toggle_task = "settings page is already open" in goal.lower()

    for step in range(1, limit + 1):
        if _should_stop(stop_event, cancel_event):
            return VisionRunResult(False, step - 1, "Stopped.", cancelled=True)

        if settings_toggle_task:
            events.on_status(f"Flipping switch · step {step}")
        elif setup_wizard_active:
            events.on_status(f"Setup wizard · step {step}")
        else:
            events.on_status(f"Working · step {step}")

        try:
            capture = capture_primary_monitor()
        except Exception as exc:
            if install_task:
                from screeny.installer_window import focus_setup_window_if_present

                focus_setup_window_if_present(min_score=10)
                time.sleep(0.4)
                try:
                    capture = capture_primary_monitor()
                except Exception:
                    return VisionRunResult(
                        False,
                        step,
                        f"Couldn't capture the screen ({exc}). Try bringing the installer forward.",
                    )
            else:
                return VisionRunResult(False, step, f"Couldn't capture the screen: {exc}")
        if SETTINGS.save_screenshots:
            _save_shot(capture, step)
        screen_hash = _screen_hash(capture)
        page_sig = page_signature()

        installer_phase = _in_installer_phase(
            progress, setup_wizard_active, opened_installers
        )

        if (
            install_task
            and progress.download_clicked
            and not progress.installer_launched
            and not installer_phase
        ):
            since = download_click_ts if download_click_ts else task_started_at
            last_result, setup_wizard_active, ok = _download_wait_loop(
                download_click_ts=since,
                events=events,
                stop_event=stop_event,
                cancel_event=cancel_event,
                opened_installers=opened_installers,
                progress=progress,
            )
            if ok:
                installer_phase = True
                _try_focus_installer(progress, after_download=True)
            elif not ok and not last_result:
                progress.download_clicked = False
                download_click_ts = 0.0
                last_result = "Download never appeared — retrying the download button."
                session_log("DOWNLOAD_WAIT timeout — reset download_clicked")
            last_was_wait = False
            continue

        # Once download ran or we launched an .exe, hunt for the installer every step.
        if install_task and (progress.download_clicked or opened_installers):
            if title := _try_focus_installer(
                progress, after_download=progress.download_clicked
            ):
                if not setup_wizard_active:
                    session_log(f"installer window focused: {title}")
                setup_wizard_active = True
                installer_phase = True
                last_result = _wizard_open_message(f"Focused installer: {title}")

        if opened_installers and not progress.installer_launched:
            progress.note_installer_launched()
            setup_wizard_active = True
            installer_phase = True

        elements: list[UIElement] = []
        if SETTINGS.use_ui_tree:
            tree_budget = (
                min(SETTINGS.ui_tree_budget, 0.7) if installer_phase else SETTINGS.ui_tree_budget
            )
            elements = get_clickable_elements(
                capture.native_width,
                capture.native_height,
                time_budget=tree_budget,
                foreground_first=installer_phase,
                foreground_only=installer_phase,
            )

        browser_vp: tuple[int, int, int, int] | None = None
        if install_task and not installer_phase:
            browser_vp = browser_viewport_rect(
                capture.native_width, capture.native_height
            )
            elements = filter_elements_for_browser(
                elements,
                browser_vp,
                screen_width=capture.native_width,
                screen_height=capture.native_height,
            )
            if on_vendor_or_app_page(elements):
                elements = filter_vendor_candidates(elements, browser_vp)
            elif install_task and not looks_like_google_results(elements):
                elements = filter_vendor_candidates(elements, browser_vp)
        if escalate_state.banned_targets:
            elements = [
                e
                for e in elements
                if e.name.strip().lower() not in escalate_state.banned_targets
            ]

        if install_task:
            phase = detect_install_phase(
                goal=goal,
                elements=elements,
                download_clicked=progress.download_clicked,
                installer_launched=progress.installer_launched,
                wizard_active=setup_wizard_active,
            )
            if phase is not None:
                idx = phase_to_progress_index(phase)
                if idx is not None:
                    frac = 0.4 if phase in {
                        InstallPhase.VENDOR_PAGE,
                        InstallPhase.DOWNLOAD_WAIT,
                    } else 1.0
                    events.on_install_phase(idx, frac)

        active_goal = _wizard_goal(goal) if installer_phase else goal
        window_rect = get_foreground_window_rect() if installer_phase else None

        # Auto-click only on confirmed installer windows with exact button labels.
        if installer_phase:
            wizard_btn = find_wizard_button_target(elements)
            if wizard_btn is not None:
                ok, detail = interact_element(wizard_btn)
                if ok:
                    last_result = f"Clicked {wizard_btn.name} in the setup wizard."
                    history.append(f"step {step}: auto wizard -> {detail}")
                    events.on_action(detail)
                    session_log(f"vision step {step} auto wizard: {wizard_btn.name}")
                    last_was_wait = False
                    continue

        # Stuck on sign-in after header Play Now — go back to the download page.
        if (
            install_task
            and not installer_phase
            and progress.vendor_site_reached
            and looks_like_auth_wall(elements)
        ):
            back = execute(
                {"action": "hotkey", "keys": ["alt", "left"]},
                screen_width=capture.native_width,
                screen_height=capture.native_height,
            )
            last_result = (
                "Backed out of sign-in — click the large DOWNLOAD button on the "
                "vendor page, not Play Now in the header."
            )
            history.append(f"step {step}: auth wall -> {back.detail}")
            session_log(f"vision step {step} auth wall back")
            events.on_action(back.detail)
            last_was_wait = False
            time.sleep(SETTINGS.page_load_wait)
            continue

        # Auto-pick Windows on generic platform chooser screens (Windows vs Mac).
        if (
            goal_is_install_like(goal)
            and not progress.platform_selected
            and platform_picker_visible(elements)
        ):
            win_target = find_windows_platform_target(elements)
            if win_target is not None:
                ok, detail = interact_element(win_target)
                if ok:
                    progress.platform_selected = True
                    last_result = f"Selected {win_target.name} for Windows."
                    history.append(f"step {step}: auto platform -> {detail}")
                    events.on_action(detail)
                    last_was_wait = False
                    continue

        # Google results: click the official vendor link — not "About this result".
        if (
            install_task
            and not setup_wizard_active
            and not progress.installer_launched
            and looks_like_google_results(elements)
        ):
            hit = find_official_search_result(elements, goal)
            if hit is not None:
                prev_sig = page_sig
                ok, detail = interact_element(hit)
                if ok:
                    progress.note_vendor_site()
                    dest = url_from_link_text(hit.name)
                    SESSION.note_left_google_search(dest)
                    last_result = f"Opened official result: {hit.name[:80]}"
                    history.append(f"step {step}: auto google result -> {detail}")
                    events.on_action(detail)
                    session_log(f"vision step {step} auto google: {hit.name[:60]}")
                    wait_for_page_settle(prev_sig)
                    session_log(f"vision step {step} page settled after google")
                    last_was_wait = False
                    continue

        # Vendor site: OCR DOWNLOAD first (UIA coords are often wrong on custom pages).
        if (
            install_task
            and not setup_wizard_active
            and not progress.download_clicked
            and not progress.installer_launched
            and on_vendor_or_app_page(elements)
        ):
            ocr_result = _click_vendor_download_via_ocr(
                capture,
                step=step,
                allow_scroll=escalate_state.tier in {"ocr", "scroll", "grid", "vision"},
            )
            if ocr_result is not None and ocr_result.ok:
                last_result, download_click_ts = _after_vendor_download_click(
                    step=step,
                    detail=ocr_result.detail,
                    label="DOWNLOAD",
                    progress=progress,
                    events=events,
                    history=history,
                )
                last_was_wait = False
                continue

            dl = find_vendor_download_target(elements)
            if dl is not None:
                ok, detail = interact_element(dl)
                if ok:
                    last_result, download_click_ts = _after_vendor_download_click(
                        step=step,
                        detail=detail,
                        label=dl.name,
                        progress=progress,
                        events=events,
                        history=history,
                    )
                    last_was_wait = False
                    continue

        # Vendor CTAs only when UIA/OCR cannot see a Download button yet.
        if (
            install_task
            and not setup_wizard_active
            and not progress.download_clicked
            and not progress.installer_launched
            and on_vendor_or_app_page(elements)
            and not vendor_download_visible(elements)
            and not platform_picker_visible(elements)
        ):
            cta = find_vendor_cta_target(elements)
            if cta is not None and not is_vendor_header_cta(cta):
                ok, detail = interact_element(cta)
                if ok:
                    progress.note_vendor_site()
                    last_result = f"Clicked {cta.name} — continue toward download."
                    history.append(f"step {step}: auto vendor CTA -> {detail}")
                    events.on_action(detail)
                    session_log(f"vision step {step} auto vendor CTA: {cta.name[:60]}")
                    if cta.kind == "link":
                        wait_for_page_settle(page_sig)
                        session_log(f"vision step {step} page settled after vendor CTA")
                    last_was_wait = False
                    continue

        if last_screen_hash and screen_hash == last_screen_hash and not last_was_wait:
            unchanged_steps += 1
        else:
            unchanged_steps = 0

        # Don't quit the moment the screen looks static — pages load slowly and
        # downloads sit on the same frame. First nudge the model to adapt, and
        # only give up after it clearly can't make anything happen.
        stall_hint = ""
        if unchanged_steps >= SETTINGS.vision_stall_limit + 3:
            return VisionRunResult(
                False, step, "The screen kept not changing no matter what I tried, so I stopped."
            )
        if unchanged_steps >= 2:
            stall_hint = (
                " NOTE: the screen hasn't changed for the last couple of steps, so your "
                "previous action probably didn't do anything. Try a DIFFERENT element or "
                "target, scroll to find it, or wait if something is still loading — do not "
                "repeat the same click."
            )

        if stuck := screen_looks_stuck(elements, history):
            stall_hint = f"{stall_hint} {stuck}".strip()
        picker_hint = platform_picker_hint(elements)
        if picker_hint:
            stall_hint = f"{stall_hint} {picker_hint}".strip()
        cookie_hint = cookie_banner_hint(elements)
        if cookie_hint:
            stall_hint = f"{stall_hint} {cookie_hint}".strip()
        cta_hint = vendor_cta_hint(elements)
        if cta_hint:
            stall_hint = f"{stall_hint} {cta_hint}".strip()
        if install_task and looks_like_google_results(elements):
            stall_hint = (
                f"{stall_hint} NOTE: On Google results — click the official vendor "
                "link (company domain in the URL), not random Download snippets."
            ).strip()

        last_screen_hash = screen_hash

        min_elements = 2 if settings_toggle_task else SETTINGS.text_only_min_elements
        # Browser install: planner-only (no vision model swap). Other tasks: text when UIA is rich.
        use_text_only = (
            not installer_phase
            and not setup_wizard_active
            and (
                (install_task and not _likely_custom_ui(elements))
                or (
                    not _likely_custom_ui(elements)
                    and len(elements) >= min_elements
                )
            )
        )

        if escalate_state.tier == "stuck":
            return VisionRunResult(
                False,
                step,
                "I got stuck repeating the same action after trying every locator tier, so I stopped.",
            )

        if installer_phase:
            stall_hint = (
                f"{stall_hint} CRITICAL: The installer is already open. Do NOT click "
                "Download in Chrome — that phase is DONE. Work only in the installer "
                "window: click Install, Next, Run, or Finish."
            ).strip()

        # Installer: try OCR/window grid before a full vision LLM call (~3–8s saved).
        fast_wizard_done = False
        if installer_phase and window_rect is not None:
            for wiz_target in ("Install", "Next", "Run", "Finish", "Get Started"):
                hybrid = _resolve_with_hybrid_pointer(
                    wiz_target,
                    capture,
                    {"action": "click", "target": wiz_target},
                    window_rect=window_rect,
                )
                if hybrid is None:
                    continue
                result = execute(
                    hybrid,
                    screen_width=capture.native_width,
                    screen_height=capture.native_height,
                    image_width=capture.native_width,
                    image_height=capture.native_height,
                )
                if result.ok:
                    last_result = f"Clicked {wiz_target} in the setup window."
                    history.append(f"step {step}: fast wizard -> {wiz_target}")
                    session_log(f"vision step {step} fast wizard: {wiz_target}")
                    events.on_action(result.detail)
                    last_was_wait = False
                    fast_wizard_done = True
                    break
        if fast_wizard_done:
            continue

        on_vendor = (
            install_task
            and not installer_phase
            and not progress.download_clicked
            and on_vendor_or_app_page(elements)
        )
        if on_vendor:
            tier = register_outcome(
                escalate_state,
                _action_key_tier("ocr", escalate_state.tier, 0, 0),
                blocked=False,
            )
            if tier == "stuck":
                return VisionRunResult(
                    False,
                    step,
                    "Could not find a DOWNLOAD button on the vendor page after trying every method.",
                )
            session_log(
                f"vision step {step} vendor page: skipping LLM (tier={escalate_state.tier})"
            )
            last_result = (
                "On the vendor site — scanning for the DOWNLOAD button. "
                "Do not click Play / Sign in navigation links."
            )
            time.sleep(1.5)
            last_was_wait = False
            continue

        try:
            if use_text_only:
                decision = chat_json(
                    model=SETTINGS.planner_model,
                    messages=[
                        {"role": "system", "content": TEXT_ONLY_SYSTEM},
                        {
                            "role": "user",
                            "content": TEXT_ONLY_USER.format(
                                goal=active_goal,
                                step=step,
                                max_steps=limit,
                                history=_format_history(history),
                                last_result=last_result + stall_hint,
                                elements=format_elements(elements),
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                print("  mode:  text-only (fast)")
            else:
                decision = chat_json(
                    model=SETTINGS.vision_model,
                    messages=[
                        {"role": "system", "content": VISION_SYSTEM},
                        {
                            "role": "user",
                            "content": VISION_USER_TEMPLATE.format(
                                goal=active_goal,
                                context=session_context,
                                width=capture.width,
                                height=capture.height,
                                step=step,
                                max_steps=limit,
                                history=_format_history(history),
                                last_result=last_result + stall_hint,
                                elements=format_elements(elements),
                            ),
                            "images": [capture.to_base64_png()],
                        },
                    ],
                    temperature=SETTINGS.vision_temperature,
                )
        except OllamaError as exc:
            return VisionRunResult(False, step, str(exc))
        except json.JSONDecodeError as exc:
            return VisionRunResult(False, step, f"The planner returned invalid JSON: {exc}")

        thought = str(decision.get("thought", "")).strip()
        action_name = normalize_action_name(decision.get("action", ""))
        if not action_name:
            decision = dict(decision)
            if decision.get("label") is not None or decision.get("id") is not None:
                if decision.get("label") is None and decision.get("id") is not None:
                    decision["label"] = decision["id"]
                decision["action"] = "click"
                action_name = "click"
            elif thought and re.search(r"\b(click|download|install|wait)\b", thought, re.I):
                decision["action"] = "wait"
                decision["seconds"] = 1
                action_name = "wait"
                last_result = (
                    'Model omitted "action" — waiting 1s. Use '
                    '{"action":"click","label":N} from the numbered list.'
                )
            else:
                decision["action"] = "wait"
                decision["seconds"] = 1
                action_name = "wait"
        if thought:
            print(f"  think: {thought}")
            session_log(f"vision step {step} think: {thought[:200]}")
            events.on_thought(thought)

        if action_name == "done":
            reason = str(decision.get("reason", "All set."))
            rejected = reject_done(goal, progress, history)
            if rejected:
                print(f"  reject done: {rejected[:80]}")
                last_result = rejected
                history.append(f"step {step}: rejected premature done")
                events.on_status("Still working — not done yet")
                last_was_wait = False
                continue
            return VisionRunResult(True, step, reason)

        if action_name == "fail":
            reason = str(decision.get("reason", "I couldn't figure out the next step."))
            return VisionRunResult(False, step, reason)

        if action_name == "ask":
            question = str(decision.get("question") or decision.get("prompt") or "").strip()
            secret = bool(decision.get("secret"))
            if not question:
                question = "I need a bit of info to continue. What should I enter?"
            events.on_status(f"Needs you: {question}")
            answer = events.ask(question, secret=secret)
            if not answer:
                return VisionRunResult(
                    False, step, "I needed some info to continue but didn't get it."
                )
            if secret:
                # Type the secret directly; never send it back through the model.
                execute(
                    {"action": "type", "text": answer},
                    screen_width=capture.native_width,
                    screen_height=capture.native_height,
                )
                last_result = "Entered the value you provided into the focused field."
                history.append(f"step {step}: asked for a secret and typed it")
            else:
                last_result = f'The user answered: "{answer}". Use this to continue.'
                history.append(f"step {step}: asked the user -> got an answer")
            last_was_wait = False
            continue

        action_key = _action_key(decision)
        recent_actions.append(action_key)
        if len(recent_actions) > SETTINGS.vision_stall_limit:
            recent_actions.pop(0)
        if (
            len(recent_actions) >= SETTINGS.vision_stall_limit
            and len(set(recent_actions)) == 1
        ):
            click_x, click_y = 0, 0
            if action_name == "click":
                try:
                    click_x, click_y = resolve_coords(
                        decision,
                        screen_width=capture.native_width,
                        screen_height=capture.native_height,
                        image_width=capture.width,
                        image_height=capture.height,
                    )
                except CoordError:
                    pass
            tier = register_outcome(
                escalate_state,
                _action_key_tier(action_name, escalate_state.tier, click_x, click_y),
                blocked=False,
            )
            recent_actions.clear()
            if tier == "stuck":
                return VisionRunResult(
                    False, step, "I got stuck repeating the same action, so I stopped."
                )
            last_result = (
                f"Same action repeated — escalating locator tier to {escalate_state.tier}."
            )
            last_was_wait = False
            continue

        if action_name == "wait":
            consecutive_waits += 1
            if consecutive_waits >= SETTINGS.vision_max_waits:
                return VisionRunResult(
                    False, step, "I waited a while but nothing changed, so I stopped."
                )
        else:
            consecutive_waits = 0

        if action_name == "open_download":
            since = download_click_ts if download_click_ts else task_started_at
            opened = _auto_open_download(
                since, events, stop_event, cancel_event, opened_installers
            )
            if opened is None:
                last_result = "No finished installer found in Downloads yet — wait for the download."
            elif opened.ok:
                setup_wizard_active = True
                progress.note_installer_launched()
                last_result = _wizard_open_message(opened.detail)
                if "already opened" not in opened.detail:
                    events.on_action(opened.detail)
                history.append(f"step {step}: open_download -> {opened.detail}")
            else:
                last_result = opened.detail
            last_was_wait = False
            continue

        # After waits during an install, try launching the downloaded file automatically.
        if (
            action_name == "wait"
            and install_task
            and progress.download_clicked
            and not progress.installer_launched
        ):
            since = download_click_ts if download_click_ts else task_started_at
            opened = _auto_open_download(
                since,
                events,
                stop_event,
                cancel_event,
                opened_installers,
                timeout=8.0,
            )
            if opened is not None and opened.ok:
                setup_wizard_active = True
                progress.note_installer_launched()
                last_result = _wizard_open_message(opened.detail)
                events.on_action(opened.detail)
                history.append(f"step {step}: auto-opened installer after wait")
                continue

        if action_name == "hotkey":
            blocked, remapped = check_install_hotkey(
                decision.get("keys") or decision.get("key"),
                elements,
                install_task=install_task,
                thought=thought,
                history=history,
            )
            if blocked:
                print(f"  block hotkey: {blocked[:70]}")
                last_result = blocked
                history.append(f"step {step}: blocked useless hotkey")
                last_was_wait = False
                continue
            from screeny.actions import parse_hotkey_keys

            decision = dict(decision)
            if remapped:
                decision["keys"] = remapped
                last_result = (
                    'Remapped mistaken "backspace" to browser Back (alt+left). '
                    "Use alt+left for navigation; backspace is only for editing text."
                )
            else:
                decision["keys"] = parse_hotkey_keys(
                    decision.get("keys") or decision.get("key")
                )

        # Guard against the re-download loop: if the model clicks the exact same
        # spot it just clicked (e.g. a Download button), don't fire it again.
        if action_name == "click" and action_key == last_click_key:
            if last_click_was_download or opened_installers:
                setup_wizard_active = True
                last_result = (
                    "The installer was already downloaded and launched — do NOT click Download "
                    "again. Look for the setup/installer window and click its Install/Next/Finish "
                    "button (large primary button, often at the bottom)."
                )
            else:
                last_result = (
                    "You already clicked that exact spot, so do NOT click it again. "
                    "Pick a different element, or wait for the next screen to appear."
                )
            history.append(f"step {step}: skipped a duplicate click")
            last_was_wait = False
            continue

        clicked_download_button = False
        click_element: UIElement | None = None
        use_visual_grounding = installer_phase or _likely_custom_ui(elements)
        if action_name == "click":
            target = _click_target(decision)
            thought_low = thought.lower()

            # Model keeps trying Chrome Download after installer is open — redirect.
            if installer_phase and (
                is_browser_download_click(target, None)
                or is_browser_download_click(target, _resolve_click_element(decision, elements, capture))
                or (
                    "download" in thought_low
                    and not re.search(r"\b(install|next|finish|wizard|setup)\b", thought_low)
                )
            ):
                _try_focus_installer(progress, after_download=True)
                setup_wizard_active = True
                for wiz_target in ("Install", "Next", "Run", "Get Started", "Finish"):
                    hybrid = _resolve_with_hybrid_pointer(
                        wiz_target,
                        capture,
                        {"action": "click", "target": wiz_target},
                        window_rect=window_rect,
                    )
                    if hybrid is not None:
                        result = execute(
                            hybrid,
                            screen_width=capture.native_width,
                            screen_height=capture.native_height,
                            image_width=capture.native_width,
                            image_height=capture.native_height,
                        )
                        last_result = (
                            f"Installer is open — skipped browser Download, clicked "
                            f"{wiz_target} in the setup window."
                        )
                        history.append(f"step {step}: redirected download -> {wiz_target}")
                        session_log(f"vision step {step} redirected to {wiz_target}")
                        events.on_action(result.detail)
                        last_was_wait = False
                        continue
                last_result = (
                    "STOP — the installer is already open. Do NOT click Download in "
                    "Chrome. Look at the INSTALLER window and click its large Install "
                    "or Next button (center/bottom of that window)."
                )
                history.append(f"step {step}: blocked browser download retry")
                session_log(f"vision step {step} blocked browser download (installer open)")
                last_was_wait = False
                continue

            element = None
            if not use_visual_grounding:
                element = _resolve_click_element(decision, elements, capture)
                if element is not None and not _uia_match_trustworthy(element, target):
                    print(
                        f"  skip:  weak UIA match '{element.name}' for target '{target[:40]}'"
                    )
                    element = None
            if element is None and "label" in decision:
                element = pick_by_label(elements, decision.get("label"))
            if element is None and use_text_only:
                for candidate in (
                    str(decision.get("label") or ""),
                    target,
                    str(decision.get("thought") or ""),
                ):
                    if candidate.strip():
                        element = pick_by_label(elements, candidate)
                        if element is not None:
                            break

            # Redirect header Play Now / platform tiles to the hero Download button.
            if (
                install_task
                and on_vendor_or_app_page(elements)
                and element is not None
                and (is_vendor_header_cta(element) or is_platform_tile(element))
            ):
                dl = find_vendor_download_target(elements)
                if dl is not None:
                    element = dl
                    target = dl.name
                else:
                    ocr_result = _click_vendor_download_via_ocr(capture, step=step)
                    if ocr_result is not None and ocr_result.ok:
                        last_result, download_click_ts = _after_vendor_download_click(
                            step=step,
                            detail=ocr_result.detail,
                            label="DOWNLOAD",
                            progress=progress,
                            events=events,
                            history=history,
                        )
                        history.append(f"step {step}: redirect play-now -> ocr download")
                        session_log(f"vision step {step} redirect to ocr download")
                        last_was_wait = False
                        continue

            if is_distractor_click(target, element) or not click_helps_install_goal(
                target,
                element,
                goal,
                wizard_active=setup_wizard_active,
                installer_phase=installer_phase,
                elements=elements,
            ):
                msg = distractor_click_message(
                    target,
                    element,
                    wizard_active=setup_wizard_active,
                    installer_phase=installer_phase,
                    elements=elements,
                )
                print(f"  block: {msg[:70]}")
                session_log(f"vision step {step} block: {msg[:100]}")
                progress.off_track_clicks += 1
                last_result = msg
                history.append(f"step {step}: blocked bad click")
                block_name = (element.name if element else target) or ""
                bx, by = (element.cx, element.cy) if element else (0, 0)
                tier = register_outcome(
                    escalate_state,
                    _action_key_tier("click", escalate_state.tier, bx, by),
                    blocked=True,
                    target_name=block_name,
                )
                if tier == "stuck":
                    return VisionRunResult(
                        False,
                        step,
                        "I kept hitting blocked targets and ran out of locator tiers, so I stopped.",
                    )
                if installer_phase:
                    _try_focus_installer(progress, after_download=True)
                if progress.off_track_clicks >= 3 and not installer_phase:
                    if looks_like_google_results(elements):
                        hit = find_official_search_result(elements, goal)
                        if hit is not None:
                            ok, detail = interact_element(hit)
                            if ok:
                                progress.off_track_clicks = 0
                                progress.note_vendor_site()
                                last_result = f"Re-opened official result: {hit.name[:80]}"
                                history.append(f"step {step}: recover google -> {detail}")
                                session_log(f"vision step {step} recover google: {hit.name[:60]}")
                                events.on_action(detail)
                                last_was_wait = False
                                continue
                    return VisionRunResult(
                        False,
                        step,
                        "I kept clicking the wrong things (browser junk, not the install). Stopped.",
                    )
                last_was_wait = False
                continue

            pre_click_hash = screen_hash
            pre_elements = list(elements)
            if element is not None:
                decision = dict(decision)
                decision.pop("x_norm", None)
                decision.pop("y_norm", None)
                cx, cy = click_point_for_element(
                    element, capture.native_width, capture.native_height
                )
                decision["x"] = cx
                decision["y"] = cy
                decision["_native"] = True
                click_element = element
                if _is_vendor_download_click(
                    target,
                    element,
                    on_vendor_page=on_vendor_or_app_page(elements),
                    viewport=browser_vp,
                    click_y=cy,
                ):
                    clicked_download_button = True
                print(f"  pick:  {element.kind} '{element.name}' @ ({cx},{cy})")
            else:
                # Text-only mode: must pick a numbered label — never OCR/grid the desktop.
                if use_text_only:
                    label = decision.get("label", "?")
                    last_result = (
                        f'Could not match "{label}" to any element. Use '
                        '{"action":"click","label":N} with N from the numbered list, '
                        "or the exact element name shown in quotes."
                    )
                    history.append(f"step {step}: text-only label miss")
                    session_log(f"vision step {step} text-only label miss: {label}")
                    recent_actions.pop()  # don't count misses toward repeat-action stop
                    last_was_wait = False
                    continue

                # Custom installers / sparse UI: window OCR or grid, not full-screen OCR.
                decision = dict(decision)
                if not decision.get("target"):
                    decision["target"] = target
                hybrid = _resolve_with_hybrid_pointer(
                    target, capture, decision, window_rect=window_rect
                )
                if hybrid is not None:
                    decision = hybrid
                else:
                    grounded = _ground_with_uitars(decision, capture)
                    if grounded is not None:
                        decision = grounded
                if (
                    SETTINGS.refine_clicks
                    and not decision.get("_hybrid")
                    and not installer_phase
                ):
                    frac = 0.42 if _is_wizard_target(target) else SETTINGS.refine_frac
                    decision = _refine_click(capture, decision, frac=frac)
                try:
                    gx, gy = resolve_coords(
                        decision,
                        screen_width=capture.native_width,
                        screen_height=capture.native_height,
                        image_width=capture.width,
                        image_height=capture.height,
                    )
                except CoordError:
                    gx, gy = 0, 0
                if _is_vendor_download_click(
                    target,
                    None,
                    on_vendor_page=on_vendor_or_app_page(elements),
                    viewport=browser_vp,
                    click_y=gy,
                ):
                    clicked_download_button = True

        # Refined / element clicks are already in native pixels; skip rescaling.
        if decision.get("_native"):
            img_w, img_h = capture.native_width, capture.native_height
        else:
            img_w, img_h = capture.width, capture.height

        if action_name == "click" and _is_taskbar_zone(
            decision, capture.native_width, capture.native_height, img_w, img_h
        ):
            ocr_result = None
            if install_task and on_vendor_or_app_page(elements):
                ocr_result = _click_vendor_download_via_ocr(capture, step=step)
            if ocr_result is not None and ocr_result.ok:
                last_result, download_click_ts = _after_vendor_download_click(
                    step=step,
                    detail=ocr_result.detail,
                    label="DOWNLOAD",
                    progress=progress,
                    events=events,
                    history=history,
                )
                history.append(f"step {step}: taskbar miss -> ocr download")
                session_log(f"vision step {step} taskbar fallback ocr download")
                last_was_wait = False
                continue
            block_name = (element.name if element else target) if action_name == "click" else ""
            bx, by = (element.cx, element.cy) if element else (0, 0)
            tier = register_outcome(
                escalate_state,
                _action_key_tier("click", escalate_state.tier, bx, by),
                blocked=True,
                target_name=block_name,
            )
            if tier == "stuck":
                return VisionRunResult(
                    False,
                    step,
                    "Taskbar mis-clicks exhausted locator tiers, so I stopped.",
                )
            recent_actions.pop()
            last_result = (
                "UIA pointed at the taskbar (wrong coords for that button). "
                "Trying OCR for DOWNLOAD — do not repeat the same label."
            )
            history.append(f"step {step}: blocked taskbar click")
            session_log(f"vision step {step} blocked taskbar zone")
            last_was_wait = False
            continue

        try:
            result = execute(
                decision,
                screen_width=capture.native_width,
                screen_height=capture.native_height,
                image_width=img_w,
                image_height=img_h,
            )
        except Exception as exc:
            result = ActionResult(False, str(exc), retryable=True)

        last_result = result.detail
        print(f"  act:   {result.detail}")
        session_log(f"vision step {step} act: {action_name} -> {result.detail[:160]}")
        history.append(f"step {step}: {action_name} -> {result.detail}")
        if result.ok:
            events.on_action(result.detail)
            if action_name == "click":
                last_click_key = action_key
                last_click_was_download = clicked_download_button
                if clicked_download_button:
                    progress.note_download_click()
                    download_click_ts = time.time()
                    session_log(f"vision step {step} phase -> DOWNLOAD_WAIT (LLM locked)")
                    last_result, setup_wizard_active, ok = _download_wait_loop(
                        download_click_ts=download_click_ts,
                        events=events,
                        stop_event=stop_event,
                        cancel_event=cancel_event,
                        opened_installers=opened_installers,
                        progress=progress,
                    )
                    if ok:
                        _try_focus_installer(progress, after_download=True)
                    elif not ok and not last_result:
                        progress.download_clicked = False
                        download_click_ts = 0.0
                        last_result = "Download never appeared — retrying the download button."
                        session_log("DOWNLOAD_WAIT timeout — reset download_clicked")
                    last_was_wait = False
                    continue
                time.sleep(SETTINGS.post_click_delay)
                post_cap = capture_primary_monitor()
                post_elements = get_clickable_elements(
                    post_cap.native_width,
                    post_cap.native_height,
                    time_budget=0.6,
                )
                feedback = post_click_feedback(
                    goal,
                    before_hash=pre_click_hash,
                    after_hash=_screen_hash(post_cap),
                    before_elements=pre_elements,
                    after_elements=post_elements,
                )
                if feedback:
                    progress.off_track_clicks += 1
                    last_result = feedback
                    history.append(f"step {step}: no real progress -> {feedback[:60]}")
                elif pre_click_hash == _screen_hash(post_cap):
                    progress.no_change_clicks += 1
                    if progress.no_change_clicks >= 4:
                        return VisionRunResult(
                            False,
                            step,
                            "Clicks weren't changing the screen — I may be stuck on the wrong page.",
                        )
                else:
                    progress.no_change_clicks = 0
                    progress.off_track_clicks = max(0, progress.off_track_clicks - 1)
                if (
                    not installer_phase
                    and click_element is not None
                    and click_element.kind == "link"
                ):
                    wait_for_page_settle(page_sig)
                    session_log(f"vision step {step} page settled after link click")
                    last_was_wait = False
                    continue
        last_was_wait = action_name == "wait"

        if not result.ok:
            if result.retryable:
                consecutive_errors += 1
                if consecutive_errors >= 4:
                    return VisionRunResult(
                        False, step, f"I kept getting stuck: {result.detail}"
                    )
                last_result = f"That action failed: {result.detail}. Try a different approach."
                continue
            return VisionRunResult(False, step, result.detail)

        consecutive_errors = 0

    return VisionRunResult(False, limit, f"Reached the step limit ({limit}), so I stopped.")


def _resolve_click_element(
    decision: dict, elements: list[UIElement], capture: Capture
) -> UIElement | None:
    """Map a click decision to a real UI element when possible (exact box)."""
    if not elements:
        return None

    # 1) Model referenced an element by number.
    if "label" in decision:
        picked = pick_by_label(elements, decision.get("label"))
        if picked is not None:
            return picked

    # 2) Match the model's target text to an element, biased by its coarse guess.
    target = str(decision.get("target") or decision.get("thought") or "").strip()
    if not target:
        return None

    hint: tuple[int, int] | None = None
    try:
        from screeny.actions import resolve_coords

        hint = resolve_coords(
            decision,
            screen_width=capture.native_width,
            screen_height=capture.native_height,
            image_width=capture.width,
            image_height=capture.height,
        )
    except Exception:
        hint = None

    return best_match(elements, target, hint=hint)


_VENDOR_CTA_LABELS = (
    "Play for Free",
    "Play Free",
    "Play Now",
    "Download",
    "Download for Windows",
)


def _try_hybrid_vendor_cta(capture: Capture) -> str | None:
    """Click a visual-only vendor CTA when UIA does not expose it."""
    if not SETTINGS.use_hybrid_pointer:
        return None
    try:
        from screeny.cursor_anim import human_click
        from screeny.hybrid_pointer import find_target

        b64 = capture.to_base64_png()
        for label in _VENDOR_CTA_LABELS:
            pt = find_target(
                label,
                screenshot_b64=b64,
                screen_w=capture.native_width,
                screen_h=capture.native_height,
            )
            if pt is None:
                continue
            human_click(pt.x, pt.y)
            print(f"  pick:  hybrid/{pt.source} vendor CTA '{pt.label[:40]}' @ ({pt.x},{pt.y})")
            return f"Clicked {pt.label} (vendor CTA)"
    except Exception as exc:
        print(f"  hybrid vendor CTA skipped: {exc}")
    return None


def _is_taskbar_zone(
    decision: dict,
    native_w: int,
    native_h: int,
    img_w: int,
    img_h: int,
) -> bool:
    """True when resolved coords sit in the Windows taskbar / notification area."""
    try:
        x, y = resolve_coords(
            decision,
            screen_width=native_w,
            screen_height=native_h,
            image_width=img_w,
            image_height=img_h,
        )
    except CoordError:
        return False
    if y >= native_h * 0.9:
        return True
    if x >= native_w * 0.82 and y >= native_h * 0.82:
        return True
    return False


def _clamp_to_rect(
    x: int, y: int, rect: tuple[int, int, int, int] | None
) -> tuple[int, int]:
    if rect is None:
        return x, y
    left, top, right, bottom = rect
    margin = 8
    return (
        max(left + margin, min(x, right - margin)),
        max(top + margin, min(y, bottom - margin)),
    )


def _resolve_with_hybrid_pointer(
    target: str,
    capture: Capture,
    decision: dict,
    *,
    window_rect: tuple[int, int, int, int] | None = None,
) -> dict | None:
    if not SETTINGS.use_hybrid_pointer:
        return None
    try:
        pt = None
        if window_rect is not None:
            from screeny.window_pointer import find_in_window

            pt = find_in_window(target, capture, window_rect)
        if pt is None:
            from screeny.hybrid_pointer import find_target

            b64 = capture.to_base64_png()
            pt = find_target(
                target,
                screenshot_b64=b64,
                screen_w=capture.native_width,
                screen_h=capture.native_height,
            )
        if pt is None:
            return None
        x, y = _clamp_to_rect(
            pt.x, pt.y, window_rect,
        )
        x = max(0, min(x, capture.native_width - 1))
        y = max(0, min(y, capture.native_height - 1))
        out = dict(decision)
        out["x"] = x
        out["y"] = y
        out["_native"] = True
        out["_hybrid"] = pt.source
        scope = "window" if window_rect else "screen"
        print(
            f"  pick:  hybrid/{pt.source}/{scope} '{pt.label[:40]}' @ ({x},{y})"
        )
        return out
    except Exception as exc:
        print(f"  hybrid pointer skipped: {exc}")
        return None


def _ground_with_uitars(decision: dict, capture: Capture) -> dict | None:
    """Use UI-TARS as a high-accuracy pointer when the UI tree can't help.

    Returns the decision with normalized coords set to UI-TARS's point so the
    refine pass can zoom in around it. Returns None if UI-TARS can't locate it.
    """
    if not SETTINGS.use_uitars_fallback:
        return None
    target = str(decision.get("target") or decision.get("thought") or "").strip()
    if not target:
        return None
    try:
        from screeny.uitars_grounder import locate

        point = locate(target, capture)
    except Exception:
        return None
    if point is None:
        return None

    new_decision = dict(decision)
    for key in ("x", "y", "nx", "ny"):
        new_decision.pop(key, None)
    new_decision.pop("_native", None)
    new_decision["x_norm"] = point[0] / capture.native_width
    new_decision["y_norm"] = point[1] / capture.native_height
    print(f"  tars:  '{target[:40]}' -> {point}")
    return new_decision


def _refine_click(
    capture: Capture, decision: dict, *, frac: float | None = None
) -> dict:
    """Coarse-to-fine: zoom into the area the model pointed at and re-localize.

    Local 7B vision models are imprecise on full-screen shots. We crop a region
    around the coarse guess, upscale it, and ask for the exact point within the
    crop, then map back to native coordinates. Falls back to the coarse guess.
    """
    try:
        cx, cy = resolve_coords(
            decision,
            screen_width=capture.native_width,
            screen_height=capture.native_height,
            image_width=capture.width,
            image_height=capture.height,
        )
    except CoordError:
        return decision

    cx_norm = cx / capture.native_width
    cy_norm = cy / capture.native_height
    target = str(decision.get("target") or decision.get("thought") or "the target").strip()

    try:
        crop_b64, box = crop_around(
            capture, cx_norm, cy_norm, frac=frac or SETTINGS.refine_frac
        )
        refined = chat_json(
            model=SETTINGS.vision_model,
            messages=[
                {"role": "system", "content": VISION_REFINE_SYSTEM},
                {
                    "role": "user",
                    "content": VISION_REFINE_USER.format(target=target),
                    "images": [crop_b64],
                },
            ],
            temperature=SETTINGS.vision_temperature,
        )
    except Exception:
        return decision

    rx = _to_float(refined.get("x_norm"))
    ry = _to_float(refined.get("y_norm"))
    if rx is None or ry is None or rx < 0 or ry < 0 or rx > 1 or ry > 1:
        return decision

    left, top, right, bottom = box
    final_x = int(left + rx * (right - left))
    final_y = int(top + ry * (bottom - top))

    # Return a decision in native pixel space (image == native for this click).
    new_decision = dict(decision)
    new_decision.pop("x_norm", None)
    new_decision.pop("y_norm", None)
    new_decision.pop("nx", None)
    new_decision.pop("ny", None)
    new_decision["x"] = final_x
    new_decision["y"] = final_y
    new_decision["_native"] = True
    return new_decision


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _format_history(history: list[str]) -> str:
    if not history:
        return "No actions yet."
    recent = history[-4:]
    return "Recent actions:\n" + "\n".join(recent)


def _should_stop(
    stop_event: threading.Event | None,
    cancel_event: threading.Event | None,
) -> bool:
    if stop_event and stop_event.is_set():
        return True
    if cancel_event and cancel_event.is_set():
        return True
    return False


def _screen_hash(capture: Capture) -> str:
    thumb = capture.image.resize((64, 36))
    return hashlib.md5(thumb.tobytes()).hexdigest()


def _action_key(decision: dict) -> str:
    payload = {
        key: decision.get(key)
        for key in ("action", "label", "x", "y", "x_norm", "y_norm", "text", "keys", "amount")
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _save_shot(capture: Capture, step: int) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SETTINGS.screenshot_dir / f"{stamp}_step{step}.png"
    capture.image.save(path)
