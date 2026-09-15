from __future__ import annotations

import queue
import subprocess
import threading
import tkinter as tk
from enum import Enum
from pathlib import Path

import customtkinter as ctk

from screeny.agent import handle_command
from screeny.events import AgentEvents
from screeny.ollama_client import ensure_ollama_running
from screeny.ui.components import (
    CommandBar,
    EmptyState,
    HeroStatus,
    SettingsDrawer,
    TaskGroup,
    TaskJourney,
)
from screeny.ui.theme import Color, Font, Space
from screeny.ui.win_shape import enable_dpi_awareness, suspend_chroma_for_drag, sync_window_shape
from screeny.voice import VoiceIO, voice_available, voice_install_hint


class AgentState(str, Enum):
    OFF = "off"
    IDLE = "idle"
    LISTENING = "listening"
    WORKING = "working"
    SPEAKING = "speaking"
    ASKING = "asking"


ORB_MAP = {
    AgentState.OFF: "OFF",
    AgentState.IDLE: "IDLE",
    AgentState.LISTENING: "LISTENING",
    AgentState.WORKING: "WORKING",
    AgentState.SPEAKING: "SPEAKING",
    AgentState.ASKING: "ASKING",
}

_HEADLINES = {
    "OFF": ("Screeny", "Tap to wake"),
    "IDLE": ("Screeny · Ready", "Say \u201cHey Screeny\u201d or type below"),
    "LISTENING": ("Listening…", "Go ahead"),
    "SPEAKING": ("Screeny", "Speaking"),
    "ASKING": ("Screeny needs you", "Answer below to continue"),
}


class ScreenyApp(ctk.CTk):
    WIDTH = 380
    COLLAPSED_H = 52
    EXPANDED_H = 500
    TOP_MARGIN = 8
    AUTO_COLLAPSE_MS = 8000
    MAX_TASK_GROUPS = 20

    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        Font.init()

        self.title("Screeny")
        self.geometry(f"{self.WIDTH}x{self.COLLAPSED_H}")
        self.resizable(False, False)
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self._voice = VoiceIO()
        self._active = False
        self._expanded = False
        self._animating = False
        self._pinned = False
        self._collapse_timer: str | None = None
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._events: queue.Queue[tuple] = queue.Queue()
        self._command_queue: queue.Queue[str] = queue.Queue()
        self._task_cancel = threading.Event()
        self._task_thread: threading.Thread | None = None
        self._task_running = False
        self._ask_abort = threading.Event()
        self._awaiting_input = threading.Event()
        self._pending_ask: tuple[dict, threading.Event] | None = None
        self._mic_muted = False
        self._state = AgentState.OFF
        self._task_groups: list[TaskGroup] = []
        self._group: TaskGroup | None = None
        self._task_summary = ""
        self._substatus = ""
        self._last_command = ""
        self._drag_x = 0
        self._drag_y = 0
        self._dragging = False
        self._drag_start: tuple[int, int] | None = None
        self._custom_position = False
        self._current_h = self.COLLAPSED_H
        self._install_progress_visible = False
        self._install_active = False
        self._journey_state = (-1, 0.0)
        self._settings_visible = False
        self._ollama_connected = False
        self._shape_mode = "none"
        self._shape_timer: str | None = None

        self._agent_events = AgentEvents(
            on_plan=self._ev_plan,
            on_thought=self._ev_thought,
            on_action=self._ev_action,
            on_status=self._ev_status,
            on_substatus=self._ev_substatus,
            on_task_done=self._ev_task_done,
            on_connection=self._ev_connection,
            on_install_phase=self._ev_install_phase,
            ask=self._ev_ask,
        )

        self._ollama_ready = threading.Event()
        self._build_ui()
        self.bind("<Map>", lambda _e: self._sync_window_shape(), add="+")
        self.bind("<Configure>", self._on_root_configure, add="+")
        self._place_top_center(self.COLLAPSED_H)
        self.after(50, self._sync_window_shape)
        if __debug__:
            self.after(200, self._log_shape_debug)
        self.after(80, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", self._on_escape)
        self.bind("<Control-Shift-s>", lambda _e: self._toggle_expand())
        self.bind("<Control-Shift-S>", lambda _e: self._toggle_expand())

    # ----------------------------------------------------------------- UI build
    def _build_ui(self) -> None:
        self.pill = ctk.CTkFrame(
            self, corner_radius=Space.RADIUS_WINDOW, fg_color=Color.BG_1, border_width=0,
        )
        self.pill.pack(fill="both", expand=True)

        self.hero = HeroStatus(
            self.pill,
            on_stop=self._stop_task,
            on_pin=self._toggle_pin,
            on_settings=self._toggle_settings,
            on_toggle=self._toggle_power,
        )
        self.hero.pack(fill="x")

        self.body = ctk.CTkFrame(self.pill, fg_color="transparent")

        self.divider = ctk.CTkFrame(self.body, height=1, fg_color=Color.STROKE)

        self.journey = TaskJourney(self.body)

        self.feed = ctk.CTkScrollableFrame(
            self.body, fg_color="transparent",
            scrollbar_button_color=Color.STROKE,
            scrollbar_button_hover_color=Color.STROKE_HI,
        )
        self.feed.bind("<Configure>", self._on_feed_resize)
        self.feed._parent_canvas.bind("<Configure>", self._fit_feed, add="+")
        self.empty = EmptyState(self.feed, on_example=self._submit_command)
        self.empty.pack(fill="x")

        self.ask_panel = ctk.CTkFrame(
            self.body, corner_radius=Space.RADIUS_CARD,
            fg_color=Color.ASKING_BG, border_width=1, border_color=Color.ASKING,
        )
        self.ask_question = ctk.CTkLabel(
            self.ask_panel, text="", anchor="w", justify="left",
            font=Font.TITLE, text_color=Color.ASKING,
            wraplength=self.WIDTH - 56,
        )
        self.ask_question.pack(fill="x", padx=Space.M, pady=(Space.M, Space.S))

        ask_row = ctk.CTkFrame(self.ask_panel, fg_color="transparent")
        ask_row.pack(fill="x", padx=Space.M, pady=(0, Space.M))
        self.ask_entry = ctk.CTkEntry(
            ask_row, placeholder_text="Your answer…", height=34,
            font=Font.BODY, fg_color=Color.BG_2, border_width=0,
        )
        self.ask_entry.pack(side="left", fill="x", expand=True, padx=(0, Space.S))
        self.ask_entry.bind("<Return>", self._on_ask_submit)
        self.ask_send = ctk.CTkButton(
            ask_row, text="Send", width=54, height=34, corner_radius=8,
            fg_color=Color.ASKING, hover_color="#D9A41F",
            text_color=Color.BG_0, command=self._on_ask_submit,
        )
        self.ask_send.pack(side="left")
        self.ask_skip = ctk.CTkButton(
            ask_row, text="Skip", width=44, height=34, corner_radius=8,
            fg_color=Color.BG_3, hover_color=Color.STROKE_HI,
            command=self._on_ask_skip,
        )
        self.ask_skip.pack(side="left", padx=(Space.S, 0))

        self.settings = SettingsDrawer(
            self.body,
            info={
                "vision_model": "qwen2.5vl:7b",
                "planner_model": "llama3.2:3b",
                "voice": "Whisper + TTS",
            },
            on_open_logs=self._open_logs,
            on_mic_toggle=self._on_mic_toggle,
        )

        self.cmdbar = CommandBar(self.body, on_submit=self._submit_command)
        self.cmdbar.entry.bind("<FocusIn>", lambda _e: self._set_pinned(True))
        self.cmdbar.entry.bind(
            "<FocusOut>", lambda _e: self.after(200, self._schedule_auto_collapse),
        )

        for widget in (
            self.hero.headline, self.hero.substatus, self.hero.orb,
            self.hero.conn.master if hasattr(self.hero.conn, "master") else self.hero,
        ):
            widget.bind("<ButtonPress-1>", self._start_drag, add="+")
            widget.bind("<B1-Motion>", self._on_drag, add="+")
            widget.bind("<ButtonRelease-1>", self._on_hero_release, add="+")

        self.hero.bind("<Button-3>", self._show_context_menu)

    def _sync_window_shape(self) -> None:
        mode = sync_window_shape(
            self,
            chroma=Color.CHROMA,
            radius_window=Space.RADIUS_WINDOW,
            bg_panel=Color.BG_1,
        )
        self._shape_mode = mode

    def _log_shape_debug(self) -> None:
        import os
        if not os.environ.get("SCREENY_SHAPE_DEBUG"):
            return
        from screeny.ui.win_shape import _client_size
        w, h = _client_size(self)
        print(f"[screeny shape] mode={getattr(self, '_shape_mode', '?')} client={w}x{h} "
              f"winfo={self.winfo_width()}x{self.winfo_height()} scale={self.tk.call('tk', 'scaling')}")

    def _on_root_configure(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self:
            return
        if self._shape_timer is not None:
            self.after_cancel(self._shape_timer)
        self._shape_timer = self.after(16, self._debounced_shape)

    def _debounced_shape(self) -> None:
        self._shape_timer = None
        if not self._dragging:
            self._sync_window_shape()

    def _fit_feed(self, event=None) -> None:
        try:
            c = self.feed._parent_canvas
            c.itemconfigure(self.feed._create_window, width=c.winfo_width())
        except Exception:
            pass

    def _on_feed_resize(self, _event=None) -> None:
        wrap = max(160, self.feed.winfo_width() - 36)
        for group in self._task_groups:
            group.set_wraplength(wrap)

    # ------------------------------------------------------------- positioning
    def _place_geometry(self, height: int) -> None:
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        if self._custom_position:
            x = self.winfo_x()
            y = self.winfo_y()
        else:
            x = max(0, (screen_w - self.WIDTH) // 2)
            y = self.TOP_MARGIN
        self._current_h = height
        self.geometry(f"{self.WIDTH}x{height}+{x}+{y}")
        self.update_idletasks()
        self._sync_window_shape()

    def _place_top_center(self, height: int) -> None:
        self._place_geometry(height)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()
        self._drag_start = (event.x_root, event.y_root)

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_start:
            dx = abs(event.x_root - self._drag_start[0])
            dy = abs(event.y_root - self._drag_start[1])
            if dx + dy < 4:
                return
        if not self._dragging:
            self._dragging = True
            suspend_chroma_for_drag(
                self,
                bg_panel=Color.BG_1,
                radius_window=Space.RADIUS_WINDOW,
            )
        x = event.x_root - self._drag_x
        y = max(0, event.y_root - self._drag_y)
        self._custom_position = True
        self.geometry(f"{self.WIDTH}x{self.winfo_height()}+{x}+{y}")

    def _on_hero_release(self, event: tk.Event | None = None) -> None:
        if self._dragging:
            self._dragging = False
            self._drag_start = None
            self._sync_window_shape()
            return
        if self._drag_start:
            dx = abs((event.x_root if event else 0) - self._drag_start[0])
            dy = abs((event.y_root if event else 0) - self._drag_start[1])
            self._drag_start = None
            if dx + dy >= 4:
                return
        if not self._active:
            self._activate()
        else:
            self._toggle_expand()

    def _animate_height(self, target: int, *, on_done=None, step: int = 0, steps: int = 10) -> None:
        if step == 0:
            self._animating = True
        start = self.winfo_height()
        if step >= steps:
            self._place_geometry(target)
            self._animating = False
            if on_done:
                on_done()
            return
        t = (step + 1) / steps
        t = 1 - (1 - t) ** 3
        h = max(self.COLLAPSED_H, int(start + (target - start) * t))
        self._place_geometry(h)
        self.after(14, lambda: self._animate_height(target, on_done=on_done, step=step + 1, steps=steps))

    def _expand(self, *, animated: bool = True, auto: bool = False) -> None:
        if self._expanded:
            return
        self._cancel_auto_collapse()
        self._expanded = True
        self.body.pack(fill="both", expand=True, padx=Space.XS, pady=(0, Space.S), after=self.hero)
        self.divider.pack(fill="x", padx=Space.M, pady=(Space.XS, Space.S))
        if self._install_active and not self._install_progress_visible:
            self.journey.pack(fill="x", padx=Space.S, pady=(0, Space.S), after=self.divider)
            self._install_progress_visible = True
            idx, frac = self._journey_state
            if idx >= 0:
                self.journey.set_phase(idx, frac)
        self.feed.pack(fill="both", expand=True, padx=Space.S)
        self.cmdbar.pack(fill="x", padx=Space.M, pady=(Space.S, Space.M))
        self.hero.set_pinned(self._pinned)
        if animated:
            self._animate_height(self.EXPANDED_H)
        else:
            self._place_top_center(self.EXPANDED_H)

    def _collapse(self, *, animated: bool = True) -> None:
        if not self._expanded:
            return
        if self._pinned or self._pending_ask or self._awaiting_input.is_set():
            return

        def finish() -> None:
            self.body.pack_forget()
            self.ask_panel.pack_forget()
            if self._settings_visible:
                self.settings.pack_forget()
                self._settings_visible = False
            self._expanded = False
            self._install_progress_visible = False
            self.journey.pack_forget()

        if animated:
            self._animate_height(self.COLLAPSED_H, on_done=finish)
        else:
            finish()
            self._place_top_center(self.COLLAPSED_H)

    def _toggle_expand(self) -> None:
        if self._expanded:
            self._set_pinned(False)
            self._collapse()
        elif self._active:
            self._expand(animated=True)

    def _toggle_pin(self) -> None:
        self._set_pinned(not self._pinned)
        if self._pinned and not self._expanded:
            self._expand(animated=True)
        elif not self._pinned:
            self._schedule_auto_collapse()

    def _set_pinned(self, pinned: bool) -> None:
        self._pinned = pinned
        self.hero.set_pinned(pinned)

    def _cancel_auto_collapse(self) -> None:
        if self._collapse_timer is not None:
            self.after_cancel(self._collapse_timer)
            self._collapse_timer = None

    def _schedule_auto_collapse(self) -> None:
        if self._pinned or self._pending_ask or self._awaiting_input.is_set():
            return
        try:
            focused = self.focus_get()
            if focused in {self.cmdbar.entry, self.ask_entry}:
                return
        except (KeyError, tk.TclError):
            pass
        self._cancel_auto_collapse()
        self._collapse_timer = self.after(self.AUTO_COLLAPSE_MS, self._try_auto_collapse)

    def _try_auto_collapse(self) -> None:
        self._collapse_timer = None
        if self._pinned or self._pending_ask or self._awaiting_input.is_set():
            return
        self._collapse(animated=True)

    def _toggle_settings(self) -> None:
        if self._settings_visible:
            self.settings.pack_forget()
            self._settings_visible = False
            return
        if not self._expanded:
            self._expand(animated=True)
        self.settings.pack(fill="x", padx=Space.M, pady=Space.S, before=self.cmdbar)
        self.settings.set_connection(self._ollama_connected)
        self._settings_visible = True

    def _toggle_power(self) -> None:
        if self._active:
            self._deactivate()
        else:
            self._activate()

    def _stop_task(self) -> None:
        self._task_cancel.set()
        if self._group:
            self._group.add_card("act", "Stopped by you")
            self._group.set_result(False)

    def _open_logs(self) -> None:
        log_dir = Path(__file__).resolve().parent.parent / "data" / "debug"
        log_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(log_dir)])

    def _on_escape(self, _event=None) -> None:
        if self._expanded:
            self._set_pinned(False)
            self._collapse()

    def _show_context_menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=0, bg=Color.BG_2, fg=Color.TXT_PRIMARY,
                       activebackground=Color.BG_3)
        if self._expanded:
            menu.add_command(label="Hide panel", command=lambda: self._collapse())
        else:
            menu.add_command(label="Show panel", command=lambda: self._expand())
        menu.add_command(label="Clear activity", command=self._clear_feed)
        menu.add_command(label="Quit Screeny", command=self._on_close)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_mic_toggle(self) -> None:
        self._mic_muted = not self._mic_muted
        if self._mic_muted and self._state == AgentState.LISTENING:
            self._post("state", AgentState.IDLE)

    def _submit_command(self, text: str | None = None) -> None:
        if text is None:
            text = self.cmdbar.entry.get().strip()
        if not text:
            return
        if not self._active:
            self._activate()
        self.cmdbar.entry.delete(0, "end")
        self._last_command = text
        self._add_feed("you", text)
        self._command_queue.put(text)

    def _retry_last(self) -> None:
        if self._last_command:
            self._submit_command(self._last_command)

    # --------------------------------------------------------------- lifecycle
    def _activate(self) -> None:
        if self._active:
            return
        self._active = True
        self._stop_event.clear()
        self._ollama_ready.clear()
        self._substatus = "Connecting to Ollama…"
        self.hero.set_state("IDLE", "Screeny · Ready", self._substatus)
        threading.Thread(target=self._warm_ollama, daemon=True).start()
        self._worker = threading.Thread(target=self._agent_loop, daemon=True)
        self._worker.start()
        self._queue_speak("Screeny online. What would you like me to do?", wait=False)

    def _warm_ollama(self) -> None:
        try:
            ensure_ollama_running()
            self._post("conn", True)
        except Exception as exc:
            self._post("conn", False)
            self._post("feed", ("error", str(exc)))
        finally:
            self._ollama_ready.set()
            self._post("state", AgentState.IDLE)

    def _deactivate(self) -> None:
        from screeny.context import SESSION

        self._active = False
        self._stop_event.set()
        self._task_cancel.set()
        self._ask_abort.set()
        self._awaiting_input.clear()
        self._ollama_ready.clear()
        SESSION.clear()
        self._voice.stop()
        while not self._command_queue.empty():
            try:
                self._command_queue.get_nowait()
            except queue.Empty:
                break
        self._post("state", AgentState.OFF)
        self._post("ask_hide", None)
        self._collapse(animated=True)

    # ------------------------------------------------------------- agent loop
    def _agent_loop(self) -> None:
        self._ollama_ready.wait(timeout=60)
        while self._active and not self._stop_event.is_set():
            try:
                text = self._command_queue.get(timeout=0.25)
            except queue.Empty:
                text = None

            if text:
                self._start_task(text)
                continue

            task_alive = self._task_thread is not None and self._task_thread.is_alive()
            if self._task_running or task_alive:
                self._stop_event.wait(0.15)
                continue

            if self._awaiting_input.is_set():
                self._stop_event.wait(0.15)
                continue

            if self._mic_muted:
                if self._active:
                    self._post("state", AgentState.IDLE)
                self._stop_event.wait(0.15)
                continue

            heard = self._voice.listen(
                stop_event=self._stop_event,
                on_start=lambda: self._post("state", AgentState.LISTENING),
            )
            if not self._active or self._stop_event.is_set():
                break
            if not heard:
                if self._active:
                    self._post("state", AgentState.IDLE)
                continue

            self._last_command = heard
            self._add_feed("you", heard)
            self._start_task(heard)

    def _start_task(self, text: str) -> None:
        if self._voice.should_exit(text):
            self._speak_async("Goodbye.")
            self._post("toggle_off")
            return

        self._voice.stop()
        self._task_running = True
        self._last_command = text
        self._substatus = "Starting…"

        self._ask_abort.set()
        self._ask_abort = threading.Event()
        self._awaiting_input.clear()
        self._post("ask_hide", None)

        self._task_cancel.set()
        self._task_cancel = threading.Event()
        cancel = self._task_cancel

        def run() -> None:
            self._task_running = True
            self._post("state", AgentState.WORKING)
            ok = True
            try:
                reply = handle_command(
                    text,
                    speak=self._speak_async,
                    stop_event=self._stop_event,
                    cancel_event=cancel,
                    events=self._agent_events,
                )
                self._task_running = False
                if self._active and not cancel.is_set():
                    self._speak_async(reply)
                    self._post("task_done", True)
            except Exception as exc:
                self._task_running = False
                ok = False
                if self._active and not cancel.is_set():
                    err = str(exc).strip() or exc.__class__.__name__
                    self._post("feed", ("error", err))
                    self._speak_async(f"Sorry, something went wrong. {err[:120]}")
                    self._post("task_done", False)
            finally:
                self._task_running = False
                if self._active and not cancel.is_set():
                    self._post("state", AgentState.IDLE)

        self._task_thread = threading.Thread(target=run, daemon=True)
        self._task_thread.start()

    # ----------------------------------------------------------- agent events
    def _ev_plan(self, summary: str, steps: list[str]) -> None:
        lines = summary.strip()
        if steps:
            numbered = " · ".join(steps[:4])
            lines = f"{lines} — {numbered}" if lines else numbered
        self._post("feed", ("plan", lines or "Planning…"))

    def _ev_thought(self, text: str) -> None:
        self._post("feed", ("think", _trim(text, 180)))

    def _ev_action(self, text: str) -> None:
        self._post("feed", ("act", _trim(text, 140)))

    def _ev_status(self, text: str) -> None:
        self._post("status", _trim(text, 80))

    def _ev_substatus(self, text: str) -> None:
        self._post("substatus", _trim(text, 80))

    def _ev_task_done(self, ok: bool) -> None:
        self._post("task_done", ok)

    def _ev_connection(self, ok: bool) -> None:
        self._post("conn", ok)

    def _ev_install_phase(self, idx: int, frac: float = 1.0) -> None:
        self._post("install_phase", (idx, frac))

    def _ev_ask(self, prompt: str, secret: bool = False) -> str | None:
        if not self._active:
            return None
        abort = self._ask_abort
        done = threading.Event()
        holder: dict = {"value": None}
        self._awaiting_input.set()
        self._post("ask", (prompt, bool(secret), holder, done))
        while not done.wait(0.15):
            if abort.is_set() or not self._active or self._stop_event.is_set():
                self._awaiting_input.clear()
                self._post("ask_hide", None)
                return None
        self._awaiting_input.clear()
        return holder.get("value")

    # ----------------------------------------------------------- ask panel UI
    def _show_ask(self, prompt: str, secret: bool, holder: dict, done: threading.Event) -> None:
        self._pending_ask = (holder, done)
        self._add_feed("ask", prompt)
        self.ask_question.configure(text=prompt)
        self.ask_entry.delete(0, "end")
        self.ask_entry.configure(show="*" if secret else "")
        self._set_pinned(True)
        if not self._expanded:
            self._expand(animated=True, auto=True)
        if not self.ask_panel.winfo_ismapped():
            self.ask_panel.pack(fill="x", padx=Space.M, pady=(0, Space.S), before=self.cmdbar)
        self._set_state(AgentState.ASKING)
        self.ask_entry.focus_set()

    def _hide_ask(self) -> None:
        self._pending_ask = None
        self.ask_panel.pack_forget()
        self.ask_entry.delete(0, "end")

    def _on_ask_submit(self, _event=None) -> None:
        if not self._pending_ask:
            return
        holder, done = self._pending_ask
        value = self.ask_entry.get().strip()
        holder["value"] = value or None
        masked = "••••••" if self.ask_entry.cget("show") else value
        if value:
            self._add_feed("you", masked)
        self._hide_ask()
        if self._active:
            self._set_state(AgentState.WORKING)
        self._set_pinned(False)
        self._schedule_auto_collapse()
        done.set()

    def _on_ask_skip(self) -> None:
        if not self._pending_ask:
            return
        holder, done = self._pending_ask
        holder["value"] = None
        self._hide_ask()
        if self._active:
            self._set_state(AgentState.WORKING)
        self._set_pinned(False)
        self._schedule_auto_collapse()
        done.set()

    # ----------------------------------------------------------------- speech
    def _queue_speak(self, text: str, *, wait: bool) -> None:
        done = threading.Event() if wait else None
        self._post("speak", (text, done))
        if wait and threading.current_thread() is not threading.main_thread() and done:
            done.wait(timeout=120)

    def _speak_async(self, text: str) -> None:
        self._queue_speak(text, wait=False)

    def _speak_on_main_thread(self, text: str, done: threading.Event | None = None) -> None:
        cleaned = text.strip()
        if not cleaned:
            if done:
                done.set()
            return
        self._add_feed("screeny", cleaned)
        self._state = AgentState.SPEAKING
        if self._group is None:
            self.hero.set_state("SPEAKING", "Screeny", cleaned[:60])
        else:
            self._set_state(AgentState.SPEAKING)

        def _on_done() -> None:
            self._post("speak_done", done)

        self._voice.speak(cleaned, on_done=_on_done)

    def _finish_speaking(self, done: threading.Event | None) -> None:
        if self._active and self._state == AgentState.SPEAKING:
            self._set_state(
                AgentState.WORKING if self._task_running else AgentState.IDLE
            )
        if done:
            done.set()

    # ------------------------------------------------------------- event pump
    def _post(self, kind: str, payload=None) -> None:
        self._events.put((kind, payload))

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break

            if kind == "state":
                self._set_state(payload)
            elif kind == "status":
                self._set_status_line(payload)
            elif kind == "substatus":
                self._substatus = str(payload)
                if self._state == AgentState.WORKING:
                    self._refresh_hero()
            elif kind == "speak":
                text, done = payload
                self._speak_on_main_thread(str(text), done)
            elif kind == "speak_done":
                self._finish_speaking(payload)
            elif kind == "feed":
                self._add_feed(payload[0], payload[1])
            elif kind == "install_phase":
                idx, frac = payload
                self._set_install_phase(int(idx), float(frac))
            elif kind == "ask":
                self._show_ask(*payload)
            elif kind == "ask_hide":
                self._hide_ask()
            elif kind == "toggle_off":
                self._deactivate()
            elif kind == "conn":
                self._ollama_connected = bool(payload)
                self.hero.conn.set_online(self._ollama_connected)
                self.settings.set_connection(self._ollama_connected)
                if self._active and not self._task_running:
                    self._substatus = (
                        "Connected to Ollama" if self._ollama_connected else "Ollama not running"
                    )
                    self._refresh_hero()
            elif kind == "task_done":
                ok = bool(payload)
                self._install_active = False
                if self._group:
                    self._group.set_result(ok)
                    if ok:
                        self._add_feed("ok", "All done.")
                if self._state == AgentState.IDLE:
                    self._schedule_auto_collapse()

        self.after(80, self._poll_events)

    # ----------------------------------------------------------------- visuals
    def _add_feed(self, kind: str, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        if self.empty.winfo_ismapped():
            self.empty.pack_forget()

        if kind == "you":
            if self._group and self._group._open:
                self._group.toggle()
            self._task_summary = text[:40].capitalize()
            self._group = TaskGroup(self.feed, command_text=text)
            self._group.pack(fill="x", pady=(0, Space.S))
            self._task_groups.append(self._group)
            if len(self._task_groups) > self.MAX_TASK_GROUPS:
                old = self._task_groups.pop(0)
                old.destroy()
            self._on_feed_resize()
            self._refresh_hero()
            return

        if self._group is None:
            if kind != "error":
                return
            self._group = TaskGroup(self.feed, command_text="Startup")
            self._group.pack(fill="x", pady=(0, Space.S))
            self._task_groups.append(self._group)
            self._on_feed_resize()

        self._group.add_card(
            kind, text,
            on_retry=self._retry_last if kind == "error" else None,
        )
        if kind == "error":
            self._group.set_result(False)
            if not self._expanded:
                self._expand(animated=True, auto=True)

        self.update_idletasks()
        try:
            self.feed._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _set_install_phase(self, idx: int, frac: float = 1.0) -> None:
        self._install_active = True
        self._journey_state = (idx, frac)
        try:
            if self._expanded and not self._install_progress_visible:
                self.journey.pack(fill="x", padx=Space.S, pady=(0, Space.S), after=self.divider)
                self._install_progress_visible = True
            if self._install_progress_visible:
                self.journey.set_phase(idx, frac)
        except tk.TclError:
            self._install_progress_visible = False

    def _clear_feed(self) -> None:
        for group in self._task_groups:
            group.destroy()
        self._task_groups.clear()
        self._group = None
        if not self.empty.winfo_ismapped():
            self.empty.pack(fill="x")

    def _set_status_line(self, text: str) -> None:
        if not self._active or not text:
            return
        if self._state in {AgentState.ASKING, AgentState.SPEAKING}:
            return
        self._substatus = text
        if self._state == AgentState.WORKING:
            self._refresh_hero()

    def _refresh_hero(self) -> None:
        orb = ORB_MAP.get(self._state, "IDLE")
        if self._state == AgentState.WORKING:
            head = self._task_summary or "Working…"
            sub = self._substatus or "Thinking…"
        else:
            head, sub = _HEADLINES.get(orb, ("Screeny", ""))
            if self._substatus and orb in {"IDLE", "OFF"}:
                sub = self._substatus
        self.hero.set_state(orb, head, sub)

    def _set_state(self, state: AgentState, message: str | None = None) -> None:
        if self._state == AgentState.ASKING and state not in {
            AgentState.OFF, AgentState.IDLE, AgentState.WORKING, AgentState.SPEAKING,
        } and self._pending_ask:
            return

        self._state = state
        orb = ORB_MAP.get(state, "IDLE")
        if state == AgentState.WORKING:
            head = self._task_summary or "Working…"
            sub = message or self._substatus or "Thinking…"
        else:
            head, sub = _HEADLINES.get(orb, ("Screeny", ""))
            if message:
                sub = message

        self.hero.set_state(orb, head, sub)

        if state == AgentState.ASKING:
            self._expand(animated=True, auto=True)
        elif state == AgentState.IDLE:
            if not self._task_running:
                self._substatus = ""
            self._schedule_auto_collapse()

    def _on_close(self) -> None:
        self._deactivate()
        self.destroy()


def _trim(text: str, limit: int) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def run_ui() -> int:
    if not voice_available():
        print(voice_install_hint())
        return 1

    enable_dpi_awareness()  # no-op if already set in main.py
    app = ScreenyApp()
    app.mainloop()
    return 0
