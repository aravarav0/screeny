from __future__ import annotations

import asyncio
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import speech_recognition as sr

OnSpeakHook = Callable[[str], None]
OnDoneHook = Callable[[], None]

EXIT_PHRASES = {
    "quit",
    "exit",
    "stop",
    "goodbye",
    "bye",
    "shut down",
    "shutdown",
}

# Natural Edge voices (need internet — free Microsoft neural TTS).
EDGE_VOICES = {
    "jenny": "en-US-JennyNeural",
    "aria": "en-US-AriaNeural",
    "guy": "en-US-GuyNeural",
    "sonia": "en-GB-SoniaNeural",
    "ryan": "en-GB-RyanNeural",
}


def voice_available() -> bool:
    try:
        import pyttsx3  # noqa: F401
        import speech_recognition  # noqa: F401

        return True
    except ImportError:
        return False


def voice_install_hint() -> str:
    return 'Install voice support with: pip install -e ".[voice]"'


class VoiceIO:
    def __init__(self) -> None:
        if not voice_available():
            raise RuntimeError(voice_install_hint())

        import pyttsx3
        import speech_recognition as sr

        from screeny.config import SETTINGS

        self._settings = SETTINGS
        self._recognizer = sr.Recognizer()
        self._recognizer.dynamic_energy_threshold = True
        self._recognizer.pause_threshold = 1.0
        self._recognizer.phrase_threshold = 0.25
        self._recognizer.non_speaking_duration = 0.6

        bootstrap = pyttsx3.init()
        bootstrap.setProperty("rate", SETTINGS.tts_rate)
        bootstrap.setProperty("volume", SETTINGS.tts_volume)
        self._sapi_voice_id = self._pick_sapi_voice_id(bootstrap, SETTINGS.tts_voice)
        if self._sapi_voice_id:
            bootstrap.setProperty("voice", self._sapi_voice_id)
        try:
            bootstrap.stop()
        except Exception:
            pass

        self._edge_voice = _resolve_edge_voice(SETTINGS.tts_voice)
        self._mic: sr.Microphone | None = None
        self._mic_device_index = SETTINGS.mic_device_index
        self._speak_lock = threading.Lock()
        self._cancel = threading.Event()
        self._playback_proc: subprocess.Popen[str] | None = None
        self._speak_queue: queue.Queue[
            tuple[str, OnSpeakHook | None, OnDoneHook | None, threading.Event | None]
        ] = queue.Queue()
        self._worker = threading.Thread(target=self._speak_worker, daemon=True)
        self._worker.start()

    def _pick_sapi_voice_id(self, engine, preferred: str) -> str | None:
        voices = engine.getProperty("voices")
        if not voices:
            return None

        if preferred and "Neural" not in preferred:
            preferred_lower = preferred.lower()
            for voice in voices:
                if preferred_lower in voice.id.lower() or preferred_lower in voice.name.lower():
                    return voice.id

        for hint in ("zira", "aria", "jenny", "guy", "david"):
            for voice in voices:
                if hint in voice.name.lower():
                    return voice.id

        return voices[0].id

    @property
    def voice_name(self) -> str:
        if self._settings.tts_engine.lower() == "sapi":
            import pyttsx3

            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            if self._sapi_voice_id:
                for item in voices:
                    if item.id == self._sapi_voice_id:
                        return item.name
            return "default"
        return self._edge_voice

    def stop(self) -> None:
        """Stop current speech and clear the pending queue (e.g. new command)."""
        self._cancel.set()
        self._kill_playback()
        while True:
            try:
                _, _, on_done, block_done = self._speak_queue.get_nowait()
                if on_done:
                    on_done()
                if block_done:
                    block_done.set()
            except queue.Empty:
                break
        self._cancel.clear()

    def speak(
        self,
        text: str,
        *,
        on_start: OnSpeakHook | None = None,
        on_done: OnDoneHook | None = None,
        block: bool = False,
    ) -> None:
        cleaned = _clean_for_speech(text)
        if not cleaned:
            if on_done:
                on_done()
            return

        block_done = threading.Event() if block else None
        self._speak_queue.put((cleaned, on_start, on_done, block_done))
        if block and block_done:
            block_done.wait(timeout=120)

    def _speak_worker(self) -> None:
        while True:
            cleaned, on_start, on_done, block_done = self._speak_queue.get()
            if self._cancel.is_set():
                if on_done:
                    on_done()
                if block_done:
                    block_done.set()
                continue

            print(f"Screeny: {cleaned}")
            if on_start:
                on_start(cleaned)

            try:
                with self._speak_lock:
                    if self._cancel.is_set():
                        continue
                    self._speak_now(cleaned)
            finally:
                if on_done:
                    on_done()
                if block_done:
                    block_done.set()

    def _speak_now(self, cleaned: str) -> None:
        engine = self._settings.tts_engine.lower()
        if engine in {"edge", "auto", ""}:
            if _speak_with_edge(
                cleaned,
                self._edge_voice,
                self._settings,
                cancel=self._cancel,
                set_proc=self._set_playback_proc,
                kill_proc=self._kill_playback,
            ):
                return
            if engine == "edge":
                print("Edge TTS unavailable — falling back to Windows SAPI.")

        if _speak_with_pyttsx3(cleaned, self._settings, self._sapi_voice_id):
            return
        _speak_with_powershell(cleaned, cancel=self._cancel, set_proc=self._set_playback_proc)

    def _set_playback_proc(self, proc: subprocess.Popen[str] | None) -> None:
        self._playback_proc = proc

    def _kill_playback(self) -> None:
        proc = self._playback_proc
        self._playback_proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def listen(
        self,
        *,
        stop_event: threading.Event | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> str | None:
        import speech_recognition as sr

        with self._microphone() as source:
            if on_start:
                on_start()
            print("Listening...")
            self._recognizer.adjust_for_ambient_noise(source, duration=0.8)

            deadline = self._settings.listen_timeout
            elapsed = 0.0
            chunk = 0.6
            audio = None

            while elapsed < deadline:
                if stop_event and stop_event.is_set():
                    return None
                try:
                    audio = self._recognizer.listen(
                        source,
                        timeout=min(chunk, deadline - elapsed),
                        phrase_time_limit=self._settings.phrase_limit,
                    )
                    break
                except sr.WaitTimeoutError:
                    elapsed += chunk

            if audio is None:
                return None

        try:
            text = self._recognizer.recognize_whisper(
                audio,
                model=self._settings.whisper_model,
                language=self._settings.whisper_language,
            )
        except sr.UnknownValueError:
            return None
        except sr.RequestError as exc:
            raise RuntimeError(f"Speech recognition failed: {exc}") from exc

        text = text.strip()
        if text:
            print(f"You: {text}")
        return text or None

    def should_exit(self, text: str) -> bool:
        normalized = re.sub(r"[^\w\s]", "", text.lower()).strip()
        return normalized in EXIT_PHRASES or normalized.startswith("goodbye")

    def _microphone(self) -> sr.Microphone:
        import speech_recognition as sr

        if self._mic is None:
            if self._mic_device_index is not None:
                self._mic = sr.Microphone(device_index=self._mic_device_index)
            else:
                self._mic = sr.Microphone()
        return self._mic


def _resolve_edge_voice(preferred: str) -> str:
    if not preferred:
        return EDGE_VOICES["jenny"]
    low = preferred.lower().strip()
    if low in EDGE_VOICES:
        return EDGE_VOICES[low]
    if "Neural" in preferred or preferred.startswith("en-"):
        return preferred
    return EDGE_VOICES.get(low, EDGE_VOICES["jenny"])


def _sapi_rate_to_edge(rate: int) -> str:
    """Map pyttsx3-ish WPM (150–200) to Edge rate offset like +0%."""
    offset = int((rate - 175) / 3)
    offset = max(-30, min(30, offset))
    return f"{offset:+d}%"


def _speak_with_edge(
    cleaned: str,
    voice: str,
    settings,
    *,
    cancel: threading.Event | None = None,
    set_proc: Callable[[subprocess.Popen[str] | None], None] | None = None,
    kill_proc: Callable[[], None] | None = None,
) -> bool:
    """Microsoft Edge neural TTS — natural voice, starts in ~1s. Needs internet."""
    try:
        import edge_tts
    except ImportError:
        return False

    path: str | None = None
    try:
        rate = _sapi_rate_to_edge(settings.tts_rate)

        async def _synthesize() -> str:
            communicate = edge_tts.Communicate(cleaned, voice, rate=rate)
            fd, tmp = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            await communicate.save(tmp)
            return tmp

        path = _run_async(_synthesize())
        if cancel and cancel.is_set():
            return False
        if not _play_mp3(
            path,
            volume=settings.tts_volume,
            cancel=cancel,
            set_proc=set_proc,
            kill_proc=kill_proc,
        ):
            print("Edge TTS: audio playback failed.")
            return False
        return True
    except Exception as exc:
        print(f"Edge TTS: {exc}")
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _play_mp3(
    path: str,
    *,
    volume: float = 1.0,
    cancel: threading.Event | None = None,
    set_proc: Callable[[subprocess.Popen[str] | None], None] | None = None,
    kill_proc: Callable[[], None] | None = None,
) -> bool:
    """Play an MP3 on Windows via WPF MediaPlayer (reliable for Edge TTS output)."""
    mp3 = Path(path).resolve()
    if not mp3.exists() or mp3.stat().st_size < 64:
        return False

    vol = max(0.0, min(1.0, volume))
    uri = mp3.as_uri()
    script = (
        "Add-Type -AssemblyName presentationCore; "
        "$p = New-Object System.Windows.Media.MediaPlayer; "
        f"$p.Volume = {vol:.2f}; "
        f"$p.Open([Uri]'{uri}'); "
        "$p.Play(); "
        "Start-Sleep -Milliseconds 350; "
        "$deadline = (Get-Date).AddSeconds(120); "
        "while ((Get-Date) -lt $deadline) { "
        "  if ($p.NaturalDuration.HasTimeSpan -and $p.Position -ge $p.NaturalDuration.TimeSpan) { break }; "
        "  Start-Sleep -Milliseconds 80 "
        "}; "
        "$p.Close()"
    )
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if set_proc:
        set_proc(proc)
    try:
        while proc.poll() is None:
            if cancel and cancel.is_set():
                if kill_proc:
                    kill_proc()
                else:
                    proc.terminate()
                return False
            time.sleep(0.06)
    finally:
        if set_proc:
            set_proc(None)

    if proc.returncode != 0:
        try:
            err = (proc.stderr.read() if proc.stderr else "").strip()
        except Exception:
            err = ""
        if err:
            print(f"TTS playback: {err[:200]}")
        return False
    return True


def _speak_with_pyttsx3(cleaned: str, settings, voice_id: str | None) -> bool:
    try:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", settings.tts_rate)
        engine.setProperty("volume", settings.tts_volume)
        if voice_id:
            engine.setProperty("voice", voice_id)
        engine.say(cleaned)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"TTS fallback: {exc}")
        return False


def _speak_with_powershell(
    cleaned: str,
    *,
    cancel: threading.Event | None = None,
    set_proc: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> None:
    escaped = cleaned.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{escaped}')"
    )
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if set_proc:
        set_proc(proc)
    try:
        while proc.poll() is None:
            if cancel and cancel.is_set():
                proc.terminate()
                return
            time.sleep(0.06)
    finally:
        if set_proc:
            set_proc(None)


def list_microphones() -> list[tuple[int, str]]:
    import speech_recognition as sr

    names = sr.Microphone.list_microphone_names()
    return [(index, name) for index, name in enumerate(names)]


def _clean_for_speech(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) > 320:
        text = text[:317].rstrip() + "..."
    return text
