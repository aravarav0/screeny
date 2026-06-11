from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from screeny.config import SETTINGS


class OllamaError(RuntimeError):
    pass


def ollama_status() -> tuple[bool, str]:
    url = f"{SETTINGS.ollama_host.rstrip('/')}/api/tags"
    try:
        response = httpx.get(url, timeout=3.0)
        response.raise_for_status()
        return True, "Ollama is running."
    except httpx.HTTPError:
        return False, (
            f"Ollama is not reachable at {SETTINGS.ollama_host}. "
            "Screeny will try to start it automatically."
        )


def ensure_ollama_running(*, wait_seconds: float = 60.0) -> None:
    hosts = _candidate_hosts()
    deadline = time.time() + wait_seconds

    while time.time() < deadline:
        for host in hosts:
            url = f"{host.rstrip('/')}/api/tags"
            if _ping(url):
                if host != SETTINGS.ollama_host.rstrip("/"):
                    os.environ["OLLAMA_HOST"] = host
                return
        time.sleep(0.75)

    print("Ollama not responding — trying to start it...")
    _start_ollama()

    while time.time() < deadline:
        for host in hosts:
            url = f"{host.rstrip('/')}/api/tags"
            if _ping(url):
                if host != SETTINGS.ollama_host.rstrip("/"):
                    os.environ["OLLAMA_HOST"] = host
                print("Ollama is ready.")
                return
        time.sleep(0.75)

    raise OllamaError(
        "Ollama is open but Screeny still can't talk to it. "
        "Quit Ollama completely from the system tray, open it again, "
        "wait 10 seconds, then retry."
    )


def _candidate_hosts() -> list[str]:
    configured = SETTINGS.ollama_host.rstrip("/")
    defaults = [
        configured,
        "http://127.0.0.1:11434",
        "http://localhost:11434",
    ]
    seen: set[str] = set()
    hosts: list[str] = []
    for host in defaults:
        if host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _ping(url: str) -> bool:
    try:
        response = httpx.get(url, timeout=3.0)
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def _start_ollama() -> None:
    if _start_ollama_windows():
        return

    ollama = shutil.which("ollama")
    if ollama:
        subprocess.Popen(
            [ollama, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _start_ollama_windows() -> bool:
    if os.name != "nt":
        return False

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/Ollama.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Ollama/Ollama.exe",
    ]

    ollama = shutil.which("ollama")
    if ollama:
        candidates.insert(0, Path(ollama))

    started = False
    for path in candidates:
        if not path.exists():
            continue
        try:
            subprocess.Popen(
                [str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            started = True
        except OSError:
            continue

    return started


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise OllamaError(f"Model did not return JSON: {text[:500]}")
        return json.loads(match.group(0))


def chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    format_json: bool = False,
    temperature: float = 0.2,
    num_predict: int | None = None,
) -> str:
    predict = num_predict if num_predict is not None else SETTINGS.ollama_num_predict
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": SETTINGS.ollama_num_ctx,
            "num_predict": predict,
        },
        "keep_alive": SETTINGS.ollama_keep_alive,
    }
    if format_json:
        payload["format"] = "json"

    ensure_ollama_running()
    host = os.environ.get("OLLAMA_HOST", SETTINGS.ollama_host).rstrip("/")
    url = f"{host}/api/chat"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = httpx.post(url, json=payload, timeout=180.0)
            response.raise_for_status()
            data = response.json()
            content = data.get("message", {}).get("content", "")
            if not content:
                raise OllamaError(f"Empty response from model {model}")
            return content
        except httpx.ConnectError as exc:
            last_error = exc
            ensure_ollama_running(wait_seconds=20.0)
        except httpx.TimeoutException as exc:
            raise OllamaError(
                f"Ollama timed out while running {model}. "
                "Try a shorter request or close other GPU apps."
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200]
            raise OllamaError(
                f"Ollama error ({exc.response.status_code}) for model {model}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(1.0)

    raise OllamaError(
        "Ollama didn't respond after several tries. "
        "Restart Ollama from the system tray, wait 10 seconds, then retry."
    ) from last_error


def chat_json(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.2,
    num_predict: int | None = None,
) -> dict[str, Any]:
    content = chat(
        model=model,
        messages=messages,
        format_json=True,
        temperature=temperature,
        num_predict=num_predict,
    )
    return _extract_json(content)
