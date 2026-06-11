from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Turn:
    command: str
    outcome: str
    mode: str
    timestamp: datetime = field(default_factory=datetime.now)


class SessionMemory:
    """Short-term memory so follow-up commands have context.

    Single shared instance is used across the app. Only one task runs at a
    time (the UI cancels the previous one), but access is still guarded.
    """

    def __init__(self, max_turns: int = 8) -> None:
        self._turns: deque[Turn] = deque(maxlen=max_turns)
        self._lock = threading.Lock()
        self.last_app: str | None = None
        self.last_url: str | None = None
        self.last_intent: str | None = None
        self.last_command: str | None = None
        self.last_outcome: str | None = None
        self.browser_open: bool = False

    def record(self, command: str, outcome: str, mode: str) -> None:
        with self._lock:
            self._turns.append(Turn(command=command, outcome=outcome, mode=mode))
            self.last_command = command.strip() or None
            self.last_outcome = outcome.strip() or None

    def note_app(self, app: str) -> None:
        with self._lock:
            self.last_app = app

    def note_url(self, url: str) -> None:
        with self._lock:
            self.last_url = url
            self.browser_open = True

    def note_intent(self, intent: str) -> None:
        with self._lock:
            self.last_intent = intent

    def recent_turns(self, count: int = 4) -> list[Turn]:
        with self._lock:
            return list(self._turns)[-count:]

    def transcript(self, count: int = 5) -> str:
        turns = self.recent_turns(count)
        if not turns:
            return "(no previous actions this session)"
        lines = []
        for turn in turns:
            outcome = turn.outcome.strip().replace("\n", " ")
            if len(outcome) > 220:
                outcome = outcome[:217] + "..."
            lines.append(f"- User: \"{turn.command}\" -> Screeny: {outcome}")
        return "\n".join(lines)

    def context_for_agent(self, current_command: str = "") -> str:
        """Conversation + state block for planner/vision prompts."""
        parts: list[str] = []
        transcript = self.transcript()
        if transcript != "(no previous actions this session)":
            parts.append(f"Recent conversation:\n{transcript}")
        state = self.state_summary()
        if state != "nothing opened yet this session":
            parts.append(f"On-screen state: {state}")
        if self.last_command and self.last_command != current_command.strip():
            parts.append(f"Previous request: \"{self.last_command}\"")
        if self.last_outcome and self.last_outcome != current_command.strip():
            snippet = self.last_outcome.replace("\n", " ")
            if len(snippet) > 180:
                snippet = snippet[:177] + "..."
            parts.append(f"Last result: {snippet}")
        if self.last_intent:
            parts.append(f"Note: {self.last_intent}")
        return "\n\n".join(parts)

    def state_summary(self) -> str:
        parts: list[str] = []
        if self.last_app:
            parts.append(f"last app opened: {self.last_app}")
        if self.last_url:
            parts.append(f"last web page opened: {self.last_url}")
        if self.browser_open:
            parts.append("a browser window is likely open")
        if not parts:
            return "nothing opened yet this session"
        return "; ".join(parts)

    def has_history(self) -> bool:
        with self._lock:
            return len(self._turns) > 0

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self.last_app = None
            self.last_url = None
            self.last_intent = None
            self.last_command = None
            self.last_outcome = None
            self.browser_open = False


SESSION = SessionMemory()
