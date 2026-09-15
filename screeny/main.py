from __future__ import annotations

import sys

# DPI awareness MUST be set before Tk/CTk initializes or SetWindowRgn coords drift at 150%.
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import argparse

from screeny.agent import handle_command
from screeny.config import SETTINGS
from screeny.ollama_client import ensure_ollama_running, ollama_status
from screeny.voice import VoiceIO, voice_available, voice_install_hint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screeny",
        description="Local Jarvis-style agent for Windows (Ollama + vision + tools).",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Command to run. If omitted, starts interactive mode.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Use keyboard input instead of the microphone.",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Terminal voice mode (no floating UI).",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable the floating UI and use terminal voice mode.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single command and exit.",
    )
    return parser


def _use_ui(args: argparse.Namespace) -> bool:
    if args.text or args.command or args.once:
        return False
    if args.no_ui or args.voice:
        return False
    return voice_available()


def _use_voice_mode(args: argparse.Namespace) -> bool:
    if args.text:
        return False
    if args.command:
        return False
    if args.voice or args.no_ui:
        return True
    return SETTINGS.voice_mode and voice_available()


def _warm_ollama() -> None:
    ok, message = ollama_status()
    if not ok:
        print(message)
    try:
        ensure_ollama_running()
        print("Ollama is ready.")
    except Exception as exc:
        print(f"Warning: {exc}")


def _run_text_loop(*, once: bool) -> int:
    _warm_ollama()
    print("Screeny ready.")
    print(f"Vision model: {SETTINGS.vision_model}")
    print("Move mouse to top-left corner to emergency-stop automation.")
    print("Type 'quit' to exit.\n")

    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0

        if not text:
            continue
        if text.lower() in {"quit", "exit", "q"}:
            print("Bye.")
            return 0

        try:
            reply = handle_command(text)
            print(f"Screeny: {reply}\n")
        except Exception as exc:
            print(f"Screeny: Error — {exc}\n")

        if once:
            return 0


def _run_voice_loop(*, once: bool) -> int:
    if not voice_available():
        print(voice_install_hint())
        return 1

    _warm_ollama()

    voice = VoiceIO()
    print("Screeny voice mode ready.")
    print(f"Vision model: {SETTINGS.vision_model}")
    print(f"Voice: {voice.voice_name}")
    print("Move mouse to top-left corner to emergency-stop automation.")
    print("Say 'quit' or 'goodbye' to exit.\n")

    voice.speak("Screeny online. What would you like me to do?")

    while True:
        try:
            text = voice.listen()
        except (EOFError, KeyboardInterrupt):
            voice.speak("Goodbye.")
            return 0

        if not text:
            voice.speak("I didn't catch that. Try again.")
            continue

        if voice.should_exit(text):
            voice.speak("Goodbye.")
            return 0

        try:
            reply = handle_command(text, speak=voice.speak)
            voice.speak(reply)
        except Exception as exc:
            voice.speak(f"Sorry, something went wrong. {exc}")

        if once:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command:
        text = " ".join(args.command).strip()
        print(handle_command(text))
        return 0

    if _use_ui(args):
        from screeny.ui_app import run_ui

        return run_ui()

    if _use_voice_mode(args):
        return _run_voice_loop(once=args.once)

    if SETTINGS.voice_mode and not args.text and not voice_available():
        print(voice_install_hint())
        print("Falling back to text mode.\n")

    return _run_text_loop(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
