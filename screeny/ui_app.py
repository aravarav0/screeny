from __future__ import annotations

import queue
import threading
import tkinter as tk
from enum import Enum

import customtkinter as ctk

from screeny.agent import handle_command
from screeny.events import AgentEvents
from screeny.ollama_client import ensure_ollama_running
from screeny.voice import VoiceIO, voice_available, voice_install_hint


class AgentState(str, Enum):
    OFF = "off"
    IDLE = "idle"
    LISTENING = "listening"
    WORKING = "working"
    SPEAKING = "speaking"
    ASKING = "asking"


STATE_META: dict[AgentState, tuple[str, str, str]] = {
    AgentState.OFF: ("Screeny", "Tap to wake", "#636366"),
    AgentState.IDLE: ("Ready", "Waiting", "#30d158"),
    AgentState.LISTENING: ("Listening", "Speak now", "#64d2ff"),
    AgentState.WORKING: ("Working", "On it", "#ffd60a"),
    AgentState.SPEAKING: ("Speaking", "", "#bf5af2"),
    AgentState.ASKING: ("Input", "Your answer", "#ff375f"),
}

FEED_STYLE: dict[str, tuple[str, str]] = {
    "you": ("You", "#64d2ff"),
    "plan": ("Plan", "#ffd60a"),
    "think": ("Think", "#bf5af2"),
    "act": ("Do", "#98989d"),
    "screeny": ("Screeny", "#30d158"),
    "ask": ("Ask", "#ff375f"),
    "error": ("Error", "#ff453a"),
}

# Window chroma key — areas with this exact color become fully transparent on
# Windows. Must not appear anywhere in the visible UI.
CHROMA = "#010101"
PILL = "#1c1c1e"
PILL_HOVER = "#2c2c2e"
TEXT = "#ffffff"
MUTED = "#8e8e93"
DIVIDER = "#3a3a3c"
INPUT_BG = "#2c2c2e"


class ScreenyApp(ctk.CTk):
    WIDTH = 380
    COLLAPSED_H = 48
    EXPANDED_H = 432
    TOP_MARGIN = 8
    CORNER_R = 24
    HOVER_COLLAPSE_MS = 400
    MAX_FEED_ROWS = 60

    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Screeny")
        self.geometry(f"{self.WIDTH}x{self.COLLAPSED_H}")
        self.resizable(False, False)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(fg_color=CHROMA)

        self._voice = VoiceIO()
        self._active = False
        self._expanded = False
        self._animating = False
        self._pinned = False
        self._leave_timer: str | None = None
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
        self._pulse_on = False
        self._feed_rows: list[ctk.CTkBaseClass] = []
        self._drag_x = 0
        self._drag_y = 0
        self._dragging = False
        self._custom_position = False
        self._current_h = self.COLLAPSED_H

        self._agent_events = AgentEvents(
            on_plan=self._ev_plan,
            on_thought=self._ev_thought,
            on_action=self._ev_action,
            on_status=self._ev_status,
            ask=self._ev_ask,
        )

        self._ollama_ready = threading.Event()
        self._build_ui()
        self._bind_hover()
        self._apply_chroma_transparency()
        self._place_top_center(self.COLLAPSED_H)
        self.after(80, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------------------------------------------------- UI build
    def _apply_chroma_transparency(self) -> None:
        """Make the rectangular window corners invisible on Windows."""
        try:
            self.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            return
        try:
            if hasattr(self, "_canvas"):
                self._canvas.configure(bg=CHROMA)
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        # Outer pill — fully rounded capsule; children inset so nothing clips the corners
        self.pill = ctk.CTkFrame(
            self, corner_radius=self.CORNER_R, fg_color=PILL, border_width=0,
        )
        self.pill.pack(fill="both", expand=True, padx=0, pady=0)

        # --- Notch header (always visible) ---
        self.header = ctk.CTkFrame(self.pill, fg_color="transparent", height=44)
        self.header.pack(fill="x", padx=12, pady=(8, 0))
        self.header.pack_propagate(False)

        left = ctk.CTkFrame(self.header, fg_color="transparent")
        left.pack(side="left", fill="y")

        self.status_dot = ctk.CTkLabel(
            left, text="●", width=12,
            font=ctk.CTkFont(size=14), text_color="#636366",
        )
        self.status_dot.pack(side="left", padx=(0, 6))

        titles = ctk.CTkFrame(left, fg_color="transparent")
        titles.pack(side="left")

        self.title_label = ctk.CTkLabel(
            titles, text="Screeny", anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT,
        )
        self.title_label.pack(anchor="w")

        self.subtitle_label = ctk.CTkLabel(
            titles, text="Tap to wake", anchor="w",
            font=ctk.CTkFont(size=10), text_color=MUTED,
        )
        self.subtitle_label.pack(anchor="w")

        # Waveform bars — centered in the notch when active
        self.wave_frame = ctk.CTkFrame(self.header, fg_color="transparent")
        self.wave_bars: list[ctk.CTkProgressBar] = []
        bar_heights = (0.25, 0.45, 0.7, 0.95, 0.6, 0.85, 0.4)
        for h in bar_heights:
            bar = ctk.CTkProgressBar(
                self.wave_frame, width=3, height=18, corner_radius=2,
                orientation="vertical", progress_color="#64d2ff", fg_color="#3a3a3c",
            )
            bar.set(h * 0.15)
            bar.pack(side="left", padx=2, pady=4)
            self.wave_bars.append(bar)

        right = ctk.CTkFrame(self.header, fg_color="transparent")
        right.pack(side="right")

        self.pin_btn = ctk.CTkButton(
            right, text="📌", width=28, height=28, corner_radius=14,
            font=ctk.CTkFont(size=12), fg_color="transparent",
            hover_color=PILL_HOVER, text_color=MUTED,
            command=self._toggle_pin,
        )

        self.mic_btn = ctk.CTkButton(
            right, text="🎤", width=28, height=28, corner_radius=14,
            font=ctk.CTkFont(size=13), fg_color="transparent",
            hover_color=PILL_HOVER, text_color=MUTED,
            command=self._on_mic_toggle,
        )
        self.mic_btn.pack(side="right", padx=(0, 2))

        self.toggle = ctk.CTkSwitch(
            right, text="", width=40, height=20,
            command=self._on_toggle,
            progress_color="#30d158", button_color="#ffffff",
            button_hover_color="#e5e5ea", fg_color="#3a3a3c",
        )
        self.toggle.pack(side="right", padx=(0, 4))

        # Tap title area to wake when off
        for widget in (left, titles, self.title_label, self.subtitle_label, self.status_dot):
            widget.bind("<Button-1>", self._on_wake_tap, add="+")

        self.bind("<Escape>", self._on_escape)
        self.header.bind("<Button-3>", self._show_context_menu)

        # --- Expandable body (revealed on hover) ---
        self.body = ctk.CTkFrame(self.pill, fg_color="transparent")

        self.divider = ctk.CTkFrame(self.body, height=1, fg_color=DIVIDER)
        self.divider.pack(fill="x", padx=12, pady=(4, 4))

        self.feed = ctk.CTkScrollableFrame(
            self.body, fg_color=PILL, corner_radius=16, height=228,
            scrollbar_button_color="#3a3a3c",
            scrollbar_button_hover_color="#48484a",
        )
        self.feed.pack(fill="both", expand=True, padx=8, pady=(0, 2))

        self.feed_label = ctk.CTkLabel(
            self.feed, text="Activity shows here",
            font=ctk.CTkFont(size=11), text_color="#636366",
        )

        self.ask_panel = ctk.CTkFrame(
            self.body, corner_radius=16, fg_color="#2c2c2e", border_width=0,
        )
        self.ask_question = ctk.CTkLabel(
            self.ask_panel, text="", anchor="w", justify="left",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#ff375f",
            wraplength=self.WIDTH - 56,
        )
        self.ask_question.pack(fill="x", padx=14, pady=(12, 6))

        ask_row = ctk.CTkFrame(self.ask_panel, fg_color="transparent")
        ask_row.pack(fill="x", padx=14, pady=(0, 12))
        self.ask_entry = ctk.CTkEntry(
            ask_row, placeholder_text="Your answer…", height=34,
            font=ctk.CTkFont(size=12), fg_color=INPUT_BG, border_width=0,
        )
        self.ask_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.ask_entry.bind("<Return>", self._on_ask_submit)
        self.ask_send = ctk.CTkButton(
            ask_row, text="Send", width=54, height=34, corner_radius=10,
            fg_color="#ff375f", hover_color="#d63350", command=self._on_ask_submit,
        )
        self.ask_send.pack(side="left")
        self.ask_skip = ctk.CTkButton(
            ask_row, text="Skip", width=44, height=34, corner_radius=10,
            fg_color="#3a3a3c", hover_color="#48484a", command=self._on_ask_skip,
        )
        self.ask_skip.pack(side="left", padx=(6, 0))

        footer = ctk.CTkFrame(self.body, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(2, 10))
        self.input_row = footer

        self.command_entry = ctk.CTkEntry(
            footer, placeholder_text="Type a command…", height=38,
            font=ctk.CTkFont(size=12), fg_color=INPUT_BG, border_width=0,
            corner_radius=12,
        )
        self.command_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.command_entry.bind("<Return>", self._on_submit_text)
        self.command_entry.bind("<FocusIn>", lambda _e: self._set_pinned(True))
        self.command_entry.bind("<FocusOut>", lambda _e: self.after(200, self._schedule_hover_collapse))

        self.send_button = ctk.CTkButton(
            footer, text="↑", width=38, height=38, corner_radius=12,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#0a84ff", hover_color="#0070e0", command=self._on_submit_text,
        )
        self.send_button.pack(side="right")

        self.clear_button = ctk.CTkButton(
            footer, text="Clear", width=44, height=24, corner_radius=8,
            font=ctk.CTkFont(size=10), fg_color="transparent",
            hover_color=PILL_HOVER, text_color=MUTED, command=self._clear_feed,
        )
        self.clear_button.pack(side="right", padx=(0, 6))

        # Drag from the title strip only (not the whole window — that breaks chroma key)
        self._drag_handle = ctk.CTkFrame(self.header, width=6, fg_color="transparent")
        self._drag_handle.pack(side="left", fill="y", padx=(0, 4))
        for widget in (self._drag_handle, left, titles, self.title_label, self.subtitle_label):
            widget.bind("<ButtonPress-1>", self._start_drag, add="+")
            widget.bind("<B1-Motion>", self._on_drag, add="+")
            widget.bind("<ButtonRelease-1>", self._end_drag, add="+")

    # ------------------------------------------------------------- positioning
    def _place_geometry(self, height: int, *, refresh_chroma: bool = True) -> None:
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
        if refresh_chroma and not self._dragging:
            self._apply_chroma_transparency()

    def _place_top_center(self, height: int) -> None:
        self._place_geometry(height)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()
        self._drag_start = (event.x_root, event.y_root)

    def _on_drag(self, event: tk.Event) -> None:
        if hasattr(self, "_drag_start"):
            dx = abs(event.x_root - self._drag_start[0])
            dy = abs(event.y_root - self._drag_start[1])
            if dx + dy < 4:
                return
        if not self._dragging:
            self._begin_drag_mode()
        x = event.x_root - self._drag_x
        y = max(0, event.y_root - self._drag_y)
        self._custom_position = True
        self.geometry(f"{self.winfo_width()}x{self.winfo_height()}+{x}+{y}")

    def _end_drag(self, _event: tk.Event | None = None) -> None:
        if self._dragging:
            self._end_drag_mode()

    def _begin_drag_mode(self) -> None:
        self._dragging = True
        if self._leave_timer is not None:
            self.after_cancel(self._leave_timer)
            self._leave_timer = None
        # Chroma-key + motion corrupts CustomTkinter; use solid bg while dragging.
        try:
            self.attributes("-transparentcolor", "")
        except tk.TclError:
            pass
        self.configure(fg_color=PILL)

    def _end_drag_mode(self) -> None:
        self._dragging = False
        self.configure(fg_color=CHROMA)
        self._apply_chroma_transparency()
        self.update_idletasks()

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
        t = 1 - (1 - t) ** 3  # ease-out
        h = max(self.COLLAPSED_H, int(start + (target - start) * t))
        self._place_geometry(h, refresh_chroma=False)
        self.after(14, lambda: self._animate_height(target, on_done=on_done, step=step + 1, steps=steps))

    def _expand(self, *, animated: bool = True) -> None:
        if self._expanded:
            return
        self._expanded = True
        self.body.pack(fill="both", expand=True, padx=4, pady=(0, 6), after=self.header)
        self.pin_btn.pack(side="right", padx=(4, 0))
        self._update_pin_btn()
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
            self._expanded = False
            self.pin_btn.pack_forget()

        if animated:
            self._animate_height(self.COLLAPSED_H, on_done=finish)
        else:
            finish()
            self._place_top_center(self.COLLAPSED_H)

    def _toggle_pin(self) -> None:
        self._set_pinned(not self._pinned)
        if self._pinned and not self._expanded:
            self._expand(animated=True)
        elif not self._pinned:
            self._schedule_hover_collapse()

    def _set_pinned(self, pinned: bool) -> None:
        self._pinned = pinned
        self._update_pin_btn()

    def _update_pin_btn(self) -> None:
        if not self.pin_btn.winfo_ismapped():
            return
        if self._pinned:
            self.pin_btn.configure(text="📍", text_color="#ffd60a")
        else:
            self.pin_btn.configure(text="📌", text_color=MUTED)

    def _bind_hover(self) -> None:
        targets = (
            self, self.pill, self.header, self.body, self.feed,
            self.input_row, self.command_entry, self.send_button,
        )
        for widget in targets:
            widget.bind("<Enter>", self._on_hover_enter, add="+")
            widget.bind("<Leave>", self._on_hover_leave, add="+")

    def _on_hover_enter(self, _event=None) -> None:
        if self._dragging or self._animating:
            return
        if self._leave_timer is not None:
            self.after_cancel(self._leave_timer)
            self._leave_timer = None
        if not self._expanded and not self._animating:
            self._expand(animated=True)

    def _on_hover_leave(self, _event=None) -> None:
        self._schedule_hover_collapse()

    def _schedule_hover_collapse(self) -> None:
        if self._pinned or self._pending_ask or self._awaiting_input.is_set():
            return
        if self._leave_timer is not None:
            self.after_cancel(self._leave_timer)
        self._leave_timer = self.after(self.HOVER_COLLAPSE_MS, self._try_hover_collapse)

    def _try_hover_collapse(self) -> None:
        self._leave_timer = None
        if self._dragging or self._pinned or self._pending_ask or self._awaiting_input.is_set():
            return
        try:
            focused = self.focus_get()
            if focused in {self.command_entry, self.ask_entry}:
                return
        except (KeyError, tk.TclError):
            pass
        if self._is_pointer_inside():
            return
        self._collapse(animated=True)

    def _is_pointer_inside(self) -> bool:
        x, y = self.winfo_pointerx(), self.winfo_pointery()
        rx, ry = self.winfo_rootx(), self.winfo_rooty()
        return rx <= x <= rx + self.winfo_width() and ry <= y <= ry + self.winfo_height()

    def _on_wake_tap(self, _event: tk.Event | None = None) -> None:
        if not self._active and not self.toggle.get():
            self.toggle.select()
            self._activate()

    def _on_escape(self, _event=None) -> None:
        if self._expanded:
            self._set_pinned(False)
            self._collapse()

    def _show_context_menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=0, bg=PILL, fg=TEXT, activebackground=PILL_HOVER)
        if self._expanded:
            menu.add_command(label="Hide panel", command=self._hide_panel)
        else:
            menu.add_command(label="Show panel", command=self._expand)
        menu.add_command(label="Quit Screeny", command=self._on_close)
        menu.tk_popup(event.x_root, event.y_root)

    def _hide_panel(self) -> None:
        self._set_pinned(False)
        self._collapse()

    def _show_active_panels(self) -> None:
        """Prepare the feed but stay collapsed — hover reveals the panel."""
        if not self.feed_label.winfo_ismapped():
            self.feed_label.pack(pady=24)

    def _hide_active_panels(self) -> None:
        self._set_pinned(False)
        self._collapse(animated=True)

    # ----------------------------------------------------------------- toggles
    def _on_mic_toggle(self) -> None:
        self._mic_muted = not self._mic_muted
        if self._mic_muted:
            self.mic_btn.configure(text="🔇", text_color="#ff453a")
            if self._state == AgentState.LISTENING:
                self._post("state", AgentState.IDLE)
        else:
            self.mic_btn.configure(text="🎤", text_color=MUTED)

    def _on_toggle(self) -> None:
        if self.toggle.get():
            self._activate()
        else:
            self._deactivate()

    def _on_submit_text(self, _event=None) -> None:
        text = self.command_entry.get().strip()
        if not text:
            return
        if not self._active:
            self.toggle.select()
            self._activate()
        self.command_entry.delete(0, "end")
        self._add_feed("you", text)
        self._command_queue.put(text)

    # --------------------------------------------------------------- lifecycle
    def _activate(self) -> None:
        if self._active:
            return
        self._active = True
        self._stop_event.clear()
        self._ollama_ready.clear()
        self._show_active_panels()
        self._post("state", AgentState.WORKING)
        threading.Thread(target=self._warm_ollama, daemon=True).start()
        self._worker = threading.Thread(target=self._agent_loop, daemon=True)
        self._worker.start()
        self._queue_speak("Screeny online. What would you like me to do?", wait=False)

    def _warm_ollama(self) -> None:
        try:
            ensure_ollama_running()
        except Exception as exc:
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
        self._hide_active_panels()

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

            self._add_feed("you", heard)
            self._start_task(heard)

    def _start_task(self, text: str) -> None:
        if self._voice.should_exit(text):
            self._speak_async("Goodbye.")
            self._post("toggle_off")
            return

        self._voice.stop()
        self._task_running = True

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
            except Exception as exc:
                self._task_running = False
                if self._active and not cancel.is_set():
                    err = str(exc).strip() or exc.__class__.__name__
                    self._post("feed", ("error", err))
                    self._speak_async(f"Sorry, something went wrong. {err[:120]}")
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
            self._expand(animated=True)
        if not self.ask_panel.winfo_ismapped():
            self.ask_panel.pack(fill="x", padx=14, pady=(0, 6), before=self.input_row)
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
        self._schedule_hover_collapse()
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
        self._schedule_hover_collapse()
        done.set()

    # ----------------------------------------------------------------- speech
    def _queue_speak(self, text: str, *, wait: bool) -> None:
        done = threading.Event() if wait else None
        self._post("speak", (text, done))
        if wait and threading.current_thread() is not threading.main_thread() and done:
            done.wait(timeout=120)

    def _speak_async(self, text: str) -> None:
        """Queue speech without blocking the agent — UI stays responsive."""
        self._queue_speak(text, wait=False)

    def _speak_on_main_thread(self, text: str, done: threading.Event | None = None) -> None:
        cleaned = text.strip()
        if not cleaned:
            if done:
                done.set()
            return
        self._add_feed("screeny", cleaned)
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
            elif kind == "speak":
                text, done = payload
                self._speak_on_main_thread(str(text), done)
            elif kind == "speak_done":
                self._finish_speaking(payload)
            elif kind == "feed":
                self._add_feed(payload[0], payload[1])
            elif kind == "ask":
                self._show_ask(*payload)
            elif kind == "ask_hide":
                self._hide_ask()
            elif kind == "toggle_off":
                self.toggle.deselect()
                self._deactivate()

        self.after(80, self._poll_events)

    # ----------------------------------------------------------------- visuals
    def _add_feed(self, kind: str, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        if self.feed_label.winfo_ismapped():
            self.feed_label.pack_forget()

        label, color = FEED_STYLE.get(kind, ("•", MUTED))

        row = ctk.CTkLabel(
            self.feed,
            text=f"{label}   {text}",
            anchor="w", justify="left",
            font=ctk.CTkFont(size=11),
            text_color=color,
            wraplength=self.WIDTH - 48,
        )
        row.pack(fill="x", padx=14, pady=3)

        self._feed_rows.append(row)
        if len(self._feed_rows) > self.MAX_FEED_ROWS:
            old = self._feed_rows.pop(0)
            old.destroy()

        self.update_idletasks()
        try:
            self.feed._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _clear_feed(self) -> None:
        for row in self._feed_rows:
            row.destroy()
        self._feed_rows.clear()
        if not self.feed_label.winfo_ismapped():
            self.feed_label.pack(pady=24)

    def _set_status_line(self, text: str) -> None:
        if not self._active or not text:
            return
        if self._state in {AgentState.ASKING, AgentState.SPEAKING}:
            return
        self._set_state(AgentState.WORKING, text)

    def _set_state(self, state: AgentState, message: str | None = None) -> None:
        if self._state == AgentState.ASKING and state not in {
            AgentState.OFF, AgentState.IDLE, AgentState.WORKING, AgentState.SPEAKING,
        } and self._pending_ask:
            return

        self._state = state
        title, subtitle, color = STATE_META[state]
        if message:
            subtitle = message
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=subtitle)
        self.status_dot.configure(text_color=color)

        show_wave = state in {
            AgentState.LISTENING, AgentState.WORKING, AgentState.SPEAKING,
        }
        if show_wave and self._active:
            if not self.wave_frame.winfo_ismapped():
                self.wave_frame.pack(side="left", expand=True, padx=12)
            self._animate_wave(color)
        else:
            self._pulse_on = False
            self.wave_frame.pack_forget()

    def _animate_wave(self, color: str) -> None:
        if self._state not in {AgentState.LISTENING, AgentState.WORKING, AgentState.SPEAKING}:
            return
        self._pulse_on = not self._pulse_on
        if self._state == AgentState.SPEAKING:
            levels = [0.9, 0.5, 1.0, 0.35, 0.75, 0.95, 0.55] if self._pulse_on else [
                0.4, 0.85, 0.3, 0.7, 0.5, 0.65, 0.45
            ]
        elif self._state == AgentState.LISTENING:
            levels = [0.6, 0.85, 0.45, 0.95, 0.55, 0.8, 0.5] if self._pulse_on else [
                0.3, 0.55, 0.25, 0.7, 0.35, 0.6, 0.3
            ]
        else:
            levels = [0.35, 0.5, 0.3, 0.55, 0.4, 0.45, 0.35] if self._pulse_on else [
                0.2, 0.35, 0.15, 0.4, 0.25, 0.3, 0.2
            ]
        for bar, level in zip(self.wave_bars, levels, strict=True):
            bar.set(level)
            bar.configure(progress_color=color)
        self.after(120, lambda: self._animate_wave(color))

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

    app = ScreenyApp()
    app.mainloop()
    return 0
