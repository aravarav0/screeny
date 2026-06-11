"""UIA-first control loop — no screenshots, no vision model.

Architecture borrowed from Windows-Use (MIT): read the accessibility tree,
let a small text LLM pick element numbers, act via Invoke/Toggle patterns.
Vision is only used when this loop fails (see agent.py).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from screeny.actions import ActionResult, execute, normalize_action_name
from screeny.config import SETTINGS
from screeny.context import SESSION
from screeny.events import NO_OP_EVENTS, AgentEvents
from screeny.ollama_client import OllamaError, chat_json
from screeny.progress_check import (
    TaskProgress,
    distractor_click_message,
    find_windows_platform_target,
    goal_is_install_like,
    is_distractor_click,
    reject_done,
)
from screeny.prompts import TEXT_ONLY_SYSTEM, TEXT_ONLY_USER
from screeny.screen import primary_monitor_size
from screeny.ui_grounding import (
    UIElement,
    best_match,
    format_elements,
    get_clickable_elements,
    interact_element,
    pick_by_label,
)

SpeakFn = Callable[[str], None]


@dataclass
class UiaRunResult:
    ok: bool
    steps: int
    message: str
    cancelled: bool = False
    needs_vision: bool = False


def run_uia_task(
    goal: str,
    *,
    max_steps: int | None = None,
    speak: SpeakFn | None = None,
    stop_event: threading.Event | None = None,
    cancel_event: threading.Event | None = None,
    events: AgentEvents | None = None,
) -> UiaRunResult:
    events = events or NO_OP_EVENTS
    limit = max_steps or SETTINGS.max_uia_steps
    screen_w, screen_h = primary_monitor_size()

    history: list[str] = []
    last_result = "starting"
    consecutive_errors = 0
    recent_actions: list[str] = []
    progress = TaskProgress()

    session_context = SESSION.context_for_agent(goal) or "(first request this session)"
    full_goal = goal
    if session_context and session_context not in goal:
        full_goal = f"{goal}\n\nSession context:\n{session_context}"

    for step in range(1, limit + 1):
        if _should_stop(stop_event, cancel_event):
            return UiaRunResult(False, step - 1, "Stopped.", cancelled=True)

        events.on_status(f"Using accessibility tree · step {step}")

        elements: list[UIElement] = []
        if SETTINGS.use_ui_tree:
            elements = get_clickable_elements(
                screen_w,
                screen_h,
                time_budget=SETTINGS.ui_tree_budget,
                foreground_first=True,
            )

        if len(elements) < SETTINGS.uia_min_elements:
            return UiaRunResult(
                False,
                step - 1,
                "Not enough accessible controls on screen.",
                needs_vision=True,
            )

        if goal_is_install_like(full_goal) and not progress.platform_selected:
            win_target = find_windows_platform_target(elements)
            if win_target is not None:
                ok, detail = interact_element(win_target)
                if ok:
                    progress.platform_selected = True
                    last_result = f"Selected {win_target.name} for Windows."
                    history.append(f"step {step}: auto platform -> {detail}")
                    events.on_action(detail)
                    continue

        try:
            decision = chat_json(
                model=SETTINGS.planner_model,
                messages=[
                    {"role": "system", "content": TEXT_ONLY_SYSTEM},
                    {
                        "role": "user",
                        "content": TEXT_ONLY_USER.format(
                            goal=full_goal,
                            step=step,
                            max_steps=limit,
                            history=_format_history(history),
                            last_result=last_result,
                            elements=format_elements(elements),
                        ),
                    },
                ],
                temperature=0.1,
            )
        except OllamaError as exc:
            return UiaRunResult(False, step, str(exc), needs_vision=True)

        thought = str(decision.get("thought", "")).strip()
        action_name = normalize_action_name(decision.get("action", ""))
        if thought:
            print(f"  uia think: {thought}")
            events.on_thought(thought)

        if action_name == "done":
            reason = str(decision.get("reason", "All set."))
            rejected = reject_done(goal, progress, history)
            if rejected:
                print(f"  reject done: {rejected[:80]}")
                last_result = rejected
                history.append(f"step {step}: rejected premature done")
                events.on_status("Still working — not done yet")
                continue
            return UiaRunResult(True, step, reason)

        if action_name == "fail":
            reason = str(decision.get("reason", "Couldn't find the right control."))
            return UiaRunResult(False, step, reason, needs_vision=True)

        if action_name == "ask":
            question = str(decision.get("question") or decision.get("prompt") or "").strip()
            if not question:
                question = "I need a bit of info to continue."
            events.on_status(f"Needs you: {question}")
            answer = events.ask(question, secret=bool(decision.get("secret")))
            if not answer:
                return UiaRunResult(
                    False, step, "I needed some info but didn't get it."
                )
            if decision.get("secret"):
                execute(
                    {"action": "type", "text": answer},
                    screen_width=screen_w,
                    screen_height=screen_h,
                )
                last_result = "Entered the value you provided."
            else:
                last_result = f'The user answered: "{answer}".'
            history.append(f"step {step}: asked user")
            continue

        action_key = _action_key(decision)
        recent_actions.append(action_key)
        if len(recent_actions) > SETTINGS.vision_stall_limit:
            recent_actions.pop(0)
        if (
            len(recent_actions) >= SETTINGS.vision_stall_limit
            and len(set(recent_actions)) == 1
        ):
            return UiaRunResult(
                False,
                step,
                "Got stuck repeating the same action.",
                needs_vision=True,
            )

        if action_name == "click":
            element = _resolve_click(decision, elements)
            if element is None:
                last_result = (
                    "Pick a valid element label from the list — don't guess coordinates."
                )
                history.append(f"step {step}: no element match")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return UiaRunResult(
                        False,
                        step,
                        "Couldn't match any control in the accessibility tree.",
                        needs_vision=True,
                    )
                continue
            if is_distractor_click(str(decision.get("target") or ""), element):
                last_result = distractor_click_message(
                    str(decision.get("target") or ""), element
                )
                history.append(f"step {step}: blocked bad click")
                continue
            ok, detail = interact_element(element)
            result = ActionResult(ok, detail, retryable=not ok)
        else:
            try:
                result = execute(
                    decision,
                    screen_width=screen_w,
                    screen_height=screen_h,
                )
            except Exception as exc:
                result = ActionResult(False, str(exc), retryable=True)

        last_result = result.detail
        print(f"  uia act: {result.detail}")
        history.append(f"step {step}: {action_name} -> {result.detail}")

        if result.ok:
            events.on_action(result.detail)
            consecutive_errors = 0
            time.sleep(SETTINGS.action_pause)
            continue

        if result.retryable:
            consecutive_errors += 1
            if consecutive_errors >= 4:
                return UiaRunResult(
                    False, step, f"UIA actions kept failing: {result.detail}", needs_vision=True
                )
            last_result = f"That failed: {result.detail}. Try another element."
            continue

        return UiaRunResult(False, step, result.detail, needs_vision=True)

    return UiaRunResult(
        False,
        limit,
        f"Reached the UIA step limit ({limit}).",
        needs_vision=True,
    )


def _resolve_click(decision: dict, elements: list[UIElement]) -> UIElement | None:
    if "label" in decision:
        picked = pick_by_label(elements, decision.get("label"))
        if picked is not None:
            return picked
    target = str(decision.get("target") or decision.get("thought") or "").strip()
    if target:
        return best_match(elements, target)
    return None


def _action_key(decision: dict) -> str:
    action = normalize_action_name(decision.get("action", ""))
    if action == "click":
        label = decision.get("label")
        target = decision.get("target", "")
        return f"click:{label}:{target}"
    return action


def _format_history(history: list[str]) -> str:
    if not history:
        return "(none yet)"
    return "\n".join(history[-6:])


def _should_stop(
    stop_event: threading.Event | None,
    cancel_event: threading.Event | None,
) -> bool:
    if stop_event and stop_event.is_set():
        return True
    if cancel_event and cancel_event.is_set():
        return True
    return False
