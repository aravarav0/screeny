"""Rich multi-phase plans for installs and open-ended tasks — no app-specific bias."""

from __future__ import annotations

import re


def default_phases_for(command: str, app_or_task: str = "") -> list[str]:
    """Heuristic phase list when the planner omits explicit phases."""
    text = command.lower()
    name = app_or_task or _guess_subject(text) or "the software"

    if re.search(r"\b(install|download|set up|setup)\b", text):
        return [
            f"Find the official {name} download from the vendor — avoid third-party download sites.",
            "Open the official site or the correct search result.",
            "If a platform picker appears (Windows vs Mac, etc.), choose Windows / PC.",
            "Click the main Download / Play for Free / Play Free / Get button for Windows.",
            "Wait for the installer file (.exe or .msi) to finish downloading.",
            "Run the installer from the browser download bar or Downloads folder.",
            "If SmartScreen or UAC blocks you and cannot be clicked, ask the user to confirm.",
            "Work through the setup wizard (Next, Install, agree, Finish) with sensible defaults.",
            "Confirm the app or launcher is installed — do not stop after the download alone.",
        ]

    return [
        "Understand what the user wants on the current screen.",
        "Take the next concrete action (click, type, scroll, or open).",
        "Check the screen changed; adapt if something failed.",
        "Repeat until the full request is done — not after one sub-step.",
    ]


def enrich_vision_goal(
    base_goal: str,
    command: str,
    *,
    reasoning: str = "",
    phases: list[str] | None = None,
) -> str:
    """Attach explicit thinking + phases so the vision model keeps the full arc."""
    phases = phases or default_phases_for(command)
    phase_block = "\n".join(f"  {i}. {p}" for i, p in enumerate(phases, 1))

    parts = [base_goal.strip()]
    if reasoning.strip():
        parts.append(f"Why we're doing this:\n{reasoning.strip()}")
    parts.append(
        "Full task arc (do NOT stop until ALL phases that apply are done):\n"
        f"{phase_block}"
    )
    parts.append(
        "Keep going autonomously. Downloading alone is NOT done. "
        "Opening the installer is NOT done unless setup is running. "
        "Use open_download after a browser download when appropriate."
    )
    return "\n\n".join(parts)


def format_plan_speech(
    summary: str,
    reasoning: str,
    phases: list[str],
) -> str:
    """Short TTS-friendly plan announcement."""
    lines = []
    if summary:
        lines.append(summary)
    if reasoning:
        lines.append(reasoning)
    if phases:
        preview = "; ".join(phases[:4])
        if len(phases) > 4:
            preview += f"; and {len(phases) - 4} more steps"
        lines.append(f"My plan: {preview}")
    return " ".join(lines)


def _guess_subject(text: str) -> str:
    m = re.search(
        r"\b(?:install|download|get|set up|setup)\s+(?:the\s+)?(.+?)(?:\s+please|\s+for me|$)",
        text,
    )
    if m:
        return m.group(1).strip()[:40]
    return ""
