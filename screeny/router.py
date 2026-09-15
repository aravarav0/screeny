from __future__ import annotations

import re
from dataclasses import dataclass

from screeny.browser_actions import should_use_vision_for_browser, try_browser_action
from screeny.context import SESSION
from screeny.events import NO_OP_EVENTS, AgentEvents
from screeny.install import (
    _looks_like_install,
    _should_use_planner_for_install,
    extract_install_target,
    install_app,
    try_install,
)
from screeny.task_planning import default_phases_for, enrich_vision_goal, format_plan_speech
from screeny.planner import _followthrough_goal, _wants_followthrough, run_planner
from screeny.system_actions import try_system_action
from screeny.tools import try_close_app, try_fast_tool
from screeny.ui_grounding import count_clickable_elements, grounding_available
from screeny.screen import primary_monitor_size
from screeny.config import SETTINGS


@dataclass(frozen=True)
class RouteOutcome:
    mode: str
    message: str | None = None
    vision_goal: str | None = None
    vision_max_steps: int | None = None


def route_command(
    command: str,
    events: AgentEvents | None = None,
) -> RouteOutcome:
    """Route a voice/text command to tool, plan, vision, or unknown."""
    events = events or NO_OP_EVENTS
    text = command.strip()
    if not text or len(text) < 2:
        return RouteOutcome("unknown", _unknown_message())

    if social := _social_reply(text):
        return RouteOutcome("tool", social)

    # Follow-ups that point at what's already on screen go straight to vision.
    if _is_screen_followup(text):
        return RouteOutcome(
            "vision", vision_goal=command, vision_max_steps=_vision_steps_for(command)
        )

    browser = try_browser_action(command)
    if browser is not None:
        return RouteOutcome("tool", browser.message)

    close = try_close_app(command)
    if close is not None:
        return RouteOutcome("tool", close.message)

    system = try_system_action(command)
    if system:
        if system.vision_goal:
            return RouteOutcome(
                "plan",
                system.message,
                system.vision_goal,
                system.vision_max_steps,
            )
        return RouteOutcome("tool", system.message)

    if _looks_like_install(command):
        prefix = ""
        planner_outcome = None
        if _should_use_planner_for_install(command.lower()):
            planner_outcome = run_planner(command, events=events)
            prefix = planner_outcome.message
        else:
            phases = default_phases_for(command)
            target = extract_install_target(command) or "it"
            summary = f"I'll get {target} installed."
            events.on_plan(summary, phases[:6])
            prefix = format_plan_speech(summary, phases[0] if phases else "", phases)

        install = try_install(command)
        if install:
            if install.vision_goal:
                goal = enrich_vision_goal(
                    install.vision_goal,
                    command,
                    phases=default_phases_for(command),
                )
                msg = _merge_messages(prefix, install.message)
                return RouteOutcome("plan", msg, goal, _vision_steps_for(command))
            if install.ok:
                msg = _merge_messages(prefix, install.message)
                return RouteOutcome("tool", msg or install.message)
            if _needs_vision(command):
                goal = enrich_vision_goal(
                    install.message or command,
                    command,
                    phases=default_phases_for(command),
                )
                return RouteOutcome(
                    "plan", _merge_messages(prefix, install.message), goal, _vision_steps_for(command)
                )
            return RouteOutcome("tool", _merge_messages(prefix, install.message))

        if planner_outcome and planner_outcome.handled:
            if planner_outcome.vision_goal:
                return RouteOutcome(
                    "plan", prefix, planner_outcome.vision_goal, _vision_steps_for(command)
                )
            # Never treat an install as finished just because a page opened.
            follow = _followthrough_goal(command)
            goal = enrich_vision_goal(
                follow,
                command,
                phases=default_phases_for(command),
            )
            return RouteOutcome(
                "plan",
                prefix or planner_outcome.message or "Continuing on screen.",
                goal,
                _vision_steps_for(command),
            )

        if not planner_outcome or not planner_outcome.handled:
            target = extract_install_target(command)
            if target:
                boot = install_app(target)
                if boot and boot.vision_goal:
                    goal = enrich_vision_goal(
                        boot.vision_goal,
                        command,
                        phases=default_phases_for(command),
                    )
                    return RouteOutcome(
                        "plan",
                        _merge_messages(prefix, boot.message),
                        goal,
                        _vision_steps_for(command),
                    )

        if _needs_vision(command):
            goal = enrich_vision_goal(
                command,
                command,
                phases=default_phases_for(command),
            )
            return RouteOutcome(
                "plan",
                prefix or "I'll work through the install on your screen.",
                goal,
                _vision_steps_for(command),
            )

    fast = try_fast_tool(command)
    if fast and fast.ok:
        if fast.vision_goal:
            return RouteOutcome(
                "plan", fast.message, fast.vision_goal, _vision_steps_for(command)
            )
        # A quick search/page-open for something that still needs on-screen
        # follow-through (download, sign in, play...) must keep driving via vision
        # instead of stopping the moment the page is open.
        if _opened_web(fast.message) and _wants_followthrough(command):
            return RouteOutcome(
                "plan",
                fast.message,
                _followthrough_goal(command),
                _vision_steps_for(command),
            )
        return RouteOutcome("tool", fast.message)

    if _looks_like_task(command) and not _should_skip_planner(command):
        outcome = run_planner(command, events=events)
        if outcome.handled and not outcome.vision_goal:
            return RouteOutcome("tool", outcome.message)
        if outcome.handled and outcome.vision_goal:
            return RouteOutcome(
                "plan", outcome.message, outcome.vision_goal, _vision_steps_for(command)
            )

    if _needs_vision(command):
        if _can_use_uia_first(command):
            return RouteOutcome(
                "uia", vision_goal=command, vision_max_steps=_vision_steps_for(command)
            )
        return RouteOutcome(
            "vision", vision_goal=command, vision_max_steps=_vision_steps_for(command)
        )

    # Short follow-ups with session history ("keep going", "try again", "yes")
    # should continue the current task instead of failing as unknown.
    if SESSION.has_history() and _looks_like_continuation(text):
        if _can_use_uia_first(command):
            return RouteOutcome(
                "uia", vision_goal=command, vision_max_steps=_vision_steps_for(command)
            )
        return RouteOutcome(
            "vision", vision_goal=command, vision_max_steps=_vision_steps_for(command)
        )

    return RouteOutcome("unknown", _unknown_message())


def _opened_web(message: str) -> bool:
    """True if a fast tool just opened a page or ran a search."""
    text = (message or "").lower()
    return text.startswith(("searched google", "opened youtube", "opened http", "opened "))


def _is_screen_followup(command: str) -> bool:
    text = command.strip().lower()

    # Explicit references to what's on the screen / search results.
    screen_refs = (
        r"\b(on screen|on the screen|on my screen|you (?:can )?see|that you see|"
        r"whichever|which ?ever|the right (?:one|link|option)|the correct (?:one|link)|"
        r"looks right|seems right|the best (?:one|link|result)|first (?:link|result|option)|"
        r"the link|that link|this link|that result|the result)\b"
    )
    if re.search(screen_refs, text):
        return True

    # "now click/open/select ..." style continuations only count if we have history.
    if SESSION.has_history() and re.search(
        r"\b(now|then|next|go ahead and|also|still)\b.*\b(click|open|select|press|choose|pick|scroll|type|install|download|run|finish)\b",
        text,
    ):
        return True

    # Vague continuations when something was already in progress.
    if SESSION.has_history() and _looks_like_continuation(text):
        return True

    # Pointing actions like "click on the ...", "select the ..." imply on-screen UI.
    if re.search(r"\b(click on|click the|select the|choose the|pick the|tap the)\b", text):
        return True

    return False


def _looks_like_continuation(command: str) -> bool:
    text = command.strip().lower()
    if not text:
        return False
    patterns = (
        r"^(yes|yeah|yep|ok|okay|sure|continue|keep going|go on|proceed|"
        r"try again|do it|finish it|complete it|same thing|next step)\b",
        r"\b(keep going|go on|try again|what we were doing|where we left off|"
        r"the install(?:er)?|the setup|the download|that again)\b",
        r"^(click|press|hit)\s+(install|next|finish|run|yes)\b",
    )
    return any(re.search(p, text) for p in patterns)


def _needs_vision(command: str) -> bool:
    if should_use_vision_for_browser(command):
        return True

    text = command.strip().lower()
    if not text:
        return False
    patterns = (
        r"\b(click|press|type|scroll|select|navigate|button|"
        r"field|menu|sidebar|installer|wizard|play|"
        r"search bar|address bar|save|submit|login|sign in)\b",
    )
    if any(re.search(pattern, text) for pattern in patterns):
        return True

    if re.search(r"\b(close|switch)\b", text) and re.search(
        r"\b(tab|tabs|window|chrome|edge|firefox|browser)\b", text
    ):
        return True

    return False


def _merge_messages(prefix: str, message: str | None) -> str:
    left = (prefix or "").strip()
    right = (message or "").strip()
    if left and right and right not in left:
        return f"{left} {right}".strip()
    return left or right


def _vision_steps_for(command: str) -> int:
    text = command.strip().lower()
    if _looks_like_install(text):
        return SETTINGS.max_vision_steps_install
    return SETTINGS.max_vision_steps


def _unknown_message() -> str:
    return (
        "I didn't quite get that. Try rephrasing, or say the app/site clearly — "
        "like 'open Discord', 'close Steam', or 'search for cheap flights'."
    )


_SOCIAL = re.compile(
    r"^(?:thanks?|thank you|thx|hello|hi|hey|good morning|good night|"
    r"you(?:'re| are) welcome|no problem|ok(?:ay)? thanks|appreciate it)[.!?\s]*$",
    re.I,
)


def _social_reply(command: str) -> str | None:
    text = command.strip()
    if not text or not _SOCIAL.match(text):
        return None
    low = text.lower()
    if re.search(r"\bthank", low):
        return "You're welcome!"
    if re.search(r"^(?:hi|hello|hey|good morning)", low):
        return "Hi! What would you like me to do?"
    return "Happy to help. What should I do next?"


def _looks_like_task(command: str) -> bool:
    text = command.strip().lower()
    if not text:
        return False
    verbs = (
        r"\b(open|launch|start|run|go to|show|pull up|search|find|look up|check|"
        r"click|press|type|play|navigate|get|download|install|setup|set up|close|switch|"
        r"what|who|when|where|why|how|tell me|can you|could you)\b"
    )
    return bool(re.search(verbs, text))


def _should_skip_planner(command: str) -> bool:
    text = command.strip().lower()
    if _looks_like_install(text):
        return True
    if re.search(r"\b(close|closing|shut)\b", text) and re.search(r"\b(tab|tabs)\b", text):
        return True
    return bool(re.search(r"\b(click|press|type)\b", text))


def _can_use_uia_first(command: str) -> bool:
    """Route to accessibility-tree loop before burning GPU on vision."""
    if not SETTINGS.uia_first or not grounding_available():
        return False
    text = command.strip().lower()
    # Spatial / visual references need pixels, not element lists.
    if re.search(
        r"\b(on screen|on the screen|looks right|the right one|whichever|"
        r"which ?ever|that you see|first result|second result)\b",
        text,
    ):
        return False
    try:
        w, h = primary_monitor_size()
        return (
            count_clickable_elements(
                w,
                h,
                min_count=SETTINGS.uia_min_elements,
            )
            >= SETTINGS.uia_min_elements
        )
    except Exception:
        return False
