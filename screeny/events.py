from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _noop_ask(*_args: object, **_kwargs: object) -> str | None:
    return None


@dataclass
class AgentEvents:
    """Callbacks the agent uses to narrate progress and ask the user for input.

    All callbacks are optional; defaults are no-ops so headless/CLI use is fine.
    The UI provides implementations that marshal onto the Tk main thread.

    - on_plan(summary, steps): the planner decided on a plan.
    - on_thought(text): a reasoning step (planner or vision).
    - on_action(text): an action that was performed.
    - on_status(text): a short status note.
    - on_substatus(text): one-line working headline (shown in the hero strip).
    - on_task_done(ok): task finished successfully or not.
    - on_connection(ok): Ollama reachability changed.
    - ask(prompt, secret=False) -> str | None: block and ask the user for
      input. Returns the user's answer, or None if unavailable/cancelled.
      When secret is True the UI should mask the input (passwords).
    """

    on_plan: Callable[[str, list[str]], None] = _noop
    on_thought: Callable[[str], None] = _noop
    on_action: Callable[[str], None] = _noop
    on_status: Callable[[str], None] = _noop
    on_substatus: Callable[[str], None] = _noop
    on_task_done: Callable[[bool], None] = _noop
    on_connection: Callable[[bool], None] = _noop
    on_install_phase: Callable[[int, float], None] = _noop
    ask: Callable[..., str | None] = _noop_ask


NO_OP_EVENTS = AgentEvents()
