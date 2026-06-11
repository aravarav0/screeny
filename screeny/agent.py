from __future__ import annotations

import re
import threading
from collections.abc import Callable

from screeny.config import SETTINGS
from screeny.context import SESSION
from screeny.events import NO_OP_EVENTS, AgentEvents
from screeny.ollama_client import ensure_ollama_running
from screeny.router import route_command
from screeny.tools import wait_seconds
from screeny.uia_loop import run_uia_task
from screeny.vision_loop import run_vision_task

SpeakFn = Callable[[str], None]


def handle_command(
    command: str,
    *,
    speak: SpeakFn | None = None,
    stop_event: threading.Event | None = None,
    cancel_event: threading.Event | None = None,
    events: AgentEvents | None = None,
) -> str:
    reply, mode = _handle(
        command,
        speak=speak,
        stop_event=stop_event,
        cancel_event=cancel_event,
        events=events or NO_OP_EVENTS,
    )
    SESSION.record(command, reply, mode)
    return reply


def _handle(
    command: str,
    *,
    speak: SpeakFn | None,
    stop_event: threading.Event | None,
    cancel_event: threading.Event | None,
    events: AgentEvents,
) -> tuple[str, str]:
    routed = route_command(command, events=events)
    mode = routed.mode
    message = routed.message
    vision_goal = routed.vision_goal
    vision_max_steps = routed.vision_max_steps

    if mode == "unknown":
        return message or "I didn't understand that.", mode

    if mode == "tool" and message:
        return message, mode

    goal = _vision_goal_with_context(vision_goal or command)
    prefix = message.strip() if message else ""

    try:
        ensure_ollama_running()
    except Exception as exc:
        return str(exc), mode

    if _should_stop(stop_event, cancel_event):
        return "Stopped.", mode

    if mode == "uia":
        if speak:
            speak(_brief_speech(prefix or "Working through the UI."))
        elif prefix:
            print(prefix)
        else:
            print("Using Windows accessibility tree...")
        if _should_stop(stop_event, cancel_event):
            return "Stopped.", mode
        uia = run_uia_task(
            goal,
            speak=speak,
            stop_event=stop_event,
            cancel_event=cancel_event,
            events=events,
        )
        if uia.cancelled:
            return "Stopped.", mode
        if uia.ok:
            reply = uia.message or "Done."
            return (f"{prefix} {reply}".strip() if prefix else reply), mode
        if uia.needs_vision and SETTINGS.vision_fallback:
            if speak:
                speak("I'll look at your screen for this part.")
            elif prefix:
                print(f"{prefix} Falling back to vision...")
            vision = run_vision_task(
                goal,
                max_steps=vision_max_steps,
                speak=speak,
                stop_event=stop_event,
                cancel_event=cancel_event,
                events=events,
            )
            if vision.cancelled:
                return "Stopped.", mode
            if vision.ok:
                reply = vision.message or "Done."
                return (f"{prefix} {reply}".strip() if prefix else reply), mode
            failure = vision.message or uia.message or "I couldn't finish that."
            return (f"{prefix} {failure}".strip() if prefix else failure), mode
        failure = uia.message or "I couldn't finish that."
        return (f"{prefix} {failure}".strip() if prefix else failure), mode

    if speak:
        if prefix and mode == "plan":
            speak("Got it — starting now.")
        elif prefix:
            speak(_brief_speech(prefix))
        elif vision_max_steps and vision_max_steps <= 4:
            speak("One sec — flipping that switch.")
        else:
            speak("On it.")
    elif prefix:
        print(prefix)
    else:
        print("Using vision to control the screen...")

    if mode == "plan" and vision_goal:
        wait = SETTINGS.page_load_wait
        if "settings page is open" in goal.lower():
            wait = SETTINGS.settings_page_wait
        if wait > 0:
            wait_seconds(min(wait, 1.5))

    if _should_stop(stop_event, cancel_event):
        return "Stopped.", mode

    result = run_vision_task(
        goal,
        max_steps=vision_max_steps,
        speak=speak,
        stop_event=stop_event,
        cancel_event=cancel_event,
        events=events,
    )

    if result.cancelled:
        return "Stopped.", mode

    if result.ok:
        reply = result.message or "Done."
        return (f"{prefix} {reply}".strip() if prefix else reply), mode

    failure = result.message or "I couldn't finish that."
    return (f"{prefix} {failure}".strip() if prefix else failure), mode


def _brief_speech(text: str, *, max_len: int = 90) -> str:
    """Keep TTS short — full detail stays in the on-screen feed."""
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) <= max_len:
        return text
    sentence = re.match(r"^(.+?[.!?])(?:\s|$)", text)
    if sentence and len(sentence.group(1)) <= max_len:
        return sentence.group(1)
    trimmed = text[:max_len].rsplit(" ", 1)[0]
    return f"{trimmed}…"


def _should_stop(
    stop_event: threading.Event | None,
    cancel_event: threading.Event | None,
) -> bool:
    if stop_event and stop_event.is_set():
        return True
    if cancel_event and cancel_event.is_set():
        return True
    return False


def _vision_goal_with_context(base: str) -> str:
    """Attach session memory so vision/planner follow-ups make sense."""
    base = (base or "").strip()
    if not base or "--- Session context ---" in base:
        return base
    ctx = SESSION.context_for_agent(base)
    if not ctx:
        return base
    return (
        f"{base}\n\n"
        f"--- Session context (continue from here — do NOT restart from scratch) ---\n"
        f"{ctx}"
    )
