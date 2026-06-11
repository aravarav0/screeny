from __future__ import annotations

import re
from dataclasses import dataclass

from screeny.config import SETTINGS
from screeny.context import SESSION
from screeny.events import NO_OP_EVENTS, AgentEvents
from screeny.install import install_app
from screeny.ollama_client import OllamaError, chat_json
from screeny.prompts import PLANNER_SYSTEM, PLANNER_USER_TEMPLATE
from screeny.tools import ToolResult, google_search, launch_app, wait_seconds
from screeny.task_planning import (
    default_phases_for,
    enrich_vision_goal,
    format_plan_speech,
)
from screeny.session_log import log as session_log
from screeny.url_safety import compact_planner_steps, planner_open_url
from screeny.window_control import close_app


@dataclass
class PlanOutcome:
    handled: bool
    message: str
    vision_goal: str | None = None


def run_planner(command: str, events: AgentEvents | None = None) -> PlanOutcome:
    events = events or NO_OP_EVENTS
    user_prompt = PLANNER_USER_TEMPLATE.format(
        transcript=SESSION.transcript(),
        state=SESSION.state_summary(),
        command=command,
    )
    try:
        plan = chat_json(
            model=SETTINGS.planner_model,
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=SETTINGS.planner_temperature,
        )
    except OllamaError:
        return PlanOutcome(False, "", command)

    summary = str(plan.get("summary", "")).strip()
    reasoning = str(plan.get("reasoning", "")).strip()
    phases_raw = plan.get("phases") or []
    phases: list[str] = []
    if isinstance(phases_raw, list):
        phases = [str(p).strip() for p in phases_raw if str(p).strip()]
    if not phases:
        phases = default_phases_for(command)

    steps = plan.get("steps") or []
    if not isinstance(steps, list):
        steps = []

    steps = _sanitize_steps(steps)
    steps = compact_planner_steps(steps, command)

    step_descriptions = [_describe_step(s) for s in steps[:8] if isinstance(s, dict)]
    step_descriptions = [d for d in step_descriptions if d]
    plan_lines = list(phases[:6]) if phases else step_descriptions
    if summary or plan_lines:
        events.on_plan(summary or "Here's my plan.", plan_lines)

    SESSION.note_intent(f"plan: {summary[:120]}" if summary else "planned task")

    messages: list[str] = []
    vision_goal: str | None = None
    did_web = False

    for step in steps[:5]:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action", "")).lower().strip()

        if action == "vision":
            vision_goal = str(step.get("goal") or command).strip()
            continue

        if action in {"google_search", "open_url"}:
            did_web = True

        if action in {"ask_user", "ask", "request_input"}:
            prompt = str(step.get("prompt") or step.get("question") or "").strip()
            if prompt:
                events.on_status(f"Needs input: {prompt}")
                answer = events.ask(prompt, secret=bool(step.get("secret")))
                if answer:
                    SESSION.note_intent(f"user provided: {prompt}")
                    messages.append("Got it, thanks.")
            continue

        events.on_action(_describe_step(step))
        session_log(f"planner step: {_describe_step(step)}")
        result = _run_step(action, step, command)
        if result:
            messages.append(result.message)
            if result.vision_goal:
                vision_goal = result.vision_goal
            if not result.ok:
                break

    # Safety net: if the planner opened a page/search for a task that clearly
    # needs on-screen follow-through (download, sign in, click a result...) but
    # forgot to add a vision step, drive it with vision anyway instead of stopping.
    if vision_goal is None and did_web and _wants_followthrough(command):
        vision_goal = _followthrough_goal(command)

    if vision_goal:
        vision_goal = enrich_vision_goal(
            vision_goal,
            command,
            reasoning=reasoning,
            phases=phases,
        )

    if not messages and not vision_goal:
        return PlanOutcome(False, summary, command)

    combined = _combine(summary, messages)
    speech = format_plan_speech(summary, reasoning, phases)
    if speech and not combined:
        combined = speech
    elif speech and combined and speech not in combined:
        combined = f"{speech} {combined}".strip()

    if vision_goal:
        return PlanOutcome(True, combined, vision_goal)

    return PlanOutcome(True, combined or "Done.", None)


_BROWSER_APPS = {
    "chrome",
    "google chrome",
    "edge",
    "microsoft edge",
    "msedge",
    "firefox",
    "brave",
    "opera",
    "browser",
    "web browser",
    "the browser",
}


def _sanitize_steps(steps: list) -> list:
    """Drop redundant browser launches: google_search/open_url already open the
    browser in the current window, so a preceding launch_app chrome just spawns
    an extra window that covers the view."""
    has_web = any(
        isinstance(s, dict)
        and str(s.get("action", "")).lower().strip() in {"google_search", "open_url"}
        for s in steps
    )
    if not has_web:
        return steps

    cleaned: list = []
    for step in steps:
        if isinstance(step, dict):
            action = str(step.get("action", "")).lower().strip()
            if action == "launch_app":
                app = str(step.get("app", "")).lower().strip()
                if app in _BROWSER_APPS:
                    continue
        cleaned.append(step)
    return cleaned


def _wants_followthrough(command: str) -> bool:
    text = command.lower()
    return bool(
        re.search(
            r"\b(download|install|set ?up|sign ?in|log ?in|log ?on|play|buy|purchase|"
            r"checkout|order|fill|click|select|open the|go into|navigate)\b",
            text,
        )
    )


def _followthrough_goal(command: str) -> str:
    return (
        "Continue from whatever is on screen for this request: "
        f'"{command}". Step by step: if on search results, click the official vendor link. '
        "Dismiss cookie/consent banners if they block the page. If asked Windows vs Mac, pick Windows. "
        "Click Download once, WAIT for the file, then open_download or open the .exe/.msi. "
        "Work through the setup wizard (Yes/Run/Next/Install/Finish). "
        "Only return done when the installer has run or the app is installed — not when a page "
        "merely loaded or a download started. If UAC blocks you, ask the user."
    )


def _describe_step(step: dict) -> str:
    action = str(step.get("action", "")).lower().strip()
    if action == "open_url":
        return f"Open {step.get('url', 'a web page')}"
    if action == "google_search":
        return f"Google: {step.get('query', '')}"
    if action == "launch_app":
        return f"Open {step.get('app', 'an app')}"
    if action == "close_app":
        return f"Close {step.get('app', 'an app')}"
    if action == "install_app":
        return f"Install {step.get('app', 'an app')}"
    if action == "wait":
        return f"Wait {step.get('seconds', 2)}s for things to load"
    if action == "vision":
        goal = str(step.get("goal", "")).strip()
        return f"Look at the screen: {goal}" if goal else "Look at the screen"
    if action in {"respond", "say", "answer", "reply"}:
        return "Answer you"
    if action in {"ask_user", "ask", "request_input"}:
        return "Ask you for info"
    return action or ""


def _combine(summary: str, messages: list[str]) -> str:
    if not messages:
        return summary
    if len(messages) == 1:
        return messages[0]
    joined = " ".join(messages)
    return f"{summary}. {joined}".strip(". ") if summary else joined


def _run_step(action: str, step: dict, command: str) -> ToolResult | None:
    if action == "open_url":
        url = str(step.get("url", "")).strip()
        if url:
            result = planner_open_url(url, command)
            if result.ok and "Searched Google" in result.message:
                SESSION.note_intent(f"searched (blocked bad url): {url[:80]}")
            else:
                SESSION.note_url(url)
            return result
        return ToolResult(False, "Planner gave open_url without a url.")

    if action == "google_search":
        query = str(step.get("query", "")).strip()
        if query:
            SESSION.note_intent(f"searched: {query}")
            SESSION.note_url("https://www.google.com/search")
            return google_search(query)
        return ToolResult(False, "Planner gave google_search without a query.")

    if action == "launch_app":
        app = str(step.get("app", "")).strip()
        if app:
            result = launch_app(app)
            SESSION.note_app(app)
            return result
        return ToolResult(False, "Planner gave launch_app without an app name.")

    if action == "close_app":
        app = str(step.get("app", "")).strip()
        if app:
            ok, message = close_app(app)
            return ToolResult(ok, message)
        return ToolResult(False, "Planner gave close_app without an app name.")

    if action == "install_app":
        app = str(step.get("app", "")).strip()
        if app:
            return install_app(app)
        return ToolResult(False, "Planner gave install_app without an app name.")

    if action == "wait":
        seconds = _safe_float(step.get("seconds", 2), default=2.0)
        return wait_seconds(min(seconds, 5.0))

    if action in {"respond", "say", "answer", "reply"}:
        text = str(step.get("text") or step.get("message") or "").strip()
        if text:
            return ToolResult(True, text)
        return None

    return None


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
