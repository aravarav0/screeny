"""Obsidian Command widgets for Screeny. All animation via .after() — UI thread only."""

from __future__ import annotations

import math
import re
import time
import tkinter as tk

import customtkinter as ctk

from screeny.ui.theme import Color, Font, Space, ROLE_STYLE


def _canvas_bg(master) -> str:
    widget = master
    for _ in range(6):
        if widget is None:
            break
        try:
            raw = widget.cget("fg_color")
            if isinstance(raw, (list, tuple)):
                raw = raw[0]
            if raw and str(raw).lower() not in {"transparent", "none", ""}:
                return str(raw)
        except Exception:
            pass
        widget = getattr(widget, "master", None)
    return Color.BG_1


# ---------------------------------------------------------------- error copy
_ERROR_MAP = [
    (r"JSONDecodeError|Expecting value|unknown action",
     "The AI gave an unreadable reply. This usually fixes itself on retry."),
    (r"ConnectionError|ConnectTimeout|Max retries|10061|11434",
     "Can't reach Ollama. Make sure it's running, then try again."),
    (r"ReadTimeout|timed out", "The model took too long to respond."),
    (r"PermissionError|Access is denied",
     "Windows blocked that action. Screeny may need to run as administrator."),
    (r"FileNotFoundError", "A file Screeny needed wasn't where it expected."),
    (r"stuck repeating|same action", "I kept hitting the same spot, so I stopped."),
    (r"taskbar|browser chrome", "I avoided clicking somewhere unsafe and re-planned."),
]


def humanize_error(raw: str) -> str:
    for pat, msg in _ERROR_MAP:
        if re.search(pat, raw or "", re.I):
            return msg
    return "Something went wrong with that step."


# ---------------------------------------------------------------- StatusOrb
class StatusOrb(tk.Canvas):
    """State orb: ring-pulse (listening), arc (working), breathe (speaking),
    amber blink (asking), red dot (error), solid (idle/off)."""
    SIZE = 30

    def __init__(self, master, **kw):
        super().__init__(master, width=self.SIZE, height=self.SIZE,
                         bg=_canvas_bg(master), highlightthickness=0, **kw)
        self._state, self._t = "IDLE", 0.0
        self._tick()

    def set_state(self, state: str) -> None:
        self._state = state.upper()

    def _tick(self) -> None:
        self.delete("all")
        c, t = self.SIZE / 2, self._t
        color = {"OFF": Color.OFF, "IDLE": Color.ACCENT_DIM,
                 "LISTENING": Color.LISTENING, "WORKING": Color.WORKING,
                 "SPEAKING": Color.SPEAKING, "ASKING": Color.ASKING,
                 "ERROR": Color.ERROR}.get(self._state, Color.ACCENT_DIM)
        if self._state == "LISTENING":
            r = 6 + (t % 1.0) * 8
            self.create_oval(c - r, c - r, c + r, c + r, outline=color, width=2)
            self.create_oval(c - 5, c - 5, c + 5, c + 5, fill=color, outline="")
        elif self._state == "WORKING":
            a = (t * 240) % 360
            self.create_oval(c - 9, c - 9, c + 9, c + 9, outline=Color.STROKE, width=3)
            self.create_arc(c - 9, c - 9, c + 9, c + 9, start=a, extent=100,
                            style="arc", outline=color, width=3)
        elif self._state == "SPEAKING":
            r = 6 + 2 * math.sin(t * 5)
            self.create_oval(c - r, c - r, c + r, c + r, fill=color, outline="")
        elif self._state == "ASKING":
            on = (t % 2.0) < 1.4
            self.create_oval(c - 7, c - 7, c + 7, c + 7,
                             fill=color if on else Color.BG_3, outline=color, width=2)
        else:
            self.create_oval(c - 6, c - 6, c + 6, c + 6, fill=color, outline="")
            self.create_oval(c - 10, c - 10, c + 10, c + 10, outline=Color.STROKE)
        self._t += 0.033
        self.after(33, self._tick)


# ---------------------------------------------------------------- ConnectionDot
class ConnectionDot(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, width=8, height=8, bg=_canvas_bg(master),
                         highlightthickness=0, **kw)
        self.set_online(False)

    def set_online(self, ok: bool) -> None:
        self.delete("all")
        self.create_oval(1, 1, 7, 7, fill=Color.ONLINE if ok else Color.OFFLINE, outline="")


# ---------------------------------------------------------------- HeroStatus
def _ellipsize(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class HeroStatus(ctk.CTkFrame):
    """Header strip — the entire collapsed-mode surface. One glance = full truth."""
    def __init__(self, master, *, on_stop, on_pin, on_settings, on_toggle, **kw):
        super().__init__(master, fg_color="transparent", height=52, **kw)
        self.pack_propagate(False)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(side="right", padx=(0, Space.S))

        def mk(text, cmd, color=Color.TXT_SECOND):
            return ctk.CTkButton(btns, text=text, width=30, height=30, command=cmd,
                                 fg_color="transparent", hover_color=Color.BG_3,
                                 text_color=color, font=(Font.FAMILY, 13), corner_radius=8)

        self.stop_btn = mk("■", on_stop, Color.ERROR)
        self.pin_btn = mk("⌖", on_pin)
        self.gear_btn = mk("⚙", on_settings)
        self.power_btn = mk("⏻", on_toggle)
        for b in (self.pin_btn, self.gear_btn, self.power_btn):
            b.pack(side="left", padx=Space.XXS)

        self.orb = StatusOrb(self)
        self.orb.pack(side="left", padx=(Space.M, Space.S))

        text = ctk.CTkFrame(self, fg_color="transparent")
        text.pack(side="left", fill="both", expand=True)
        self.headline = ctk.CTkLabel(text, text="Screeny", font=Font.DISPLAY,
                                     text_color=Color.TXT_PRIMARY, anchor="w")
        self.headline.pack(fill="x", pady=(Space.XS, 0))
        sub = ctk.CTkFrame(text, fg_color="transparent")
        sub.pack(fill="x")
        self.conn = ConnectionDot(sub)
        self.conn.pack(side="left", pady=2)
        self.substatus = ctk.CTkLabel(sub, text="Tap to wake", font=Font.CAPTION,
                                      text_color=Color.TXT_SECOND, anchor="w")
        self.substatus.pack(side="left", padx=(Space.XS, 0), fill="x", expand=True)

    def set_state(self, state: str, headline: str, substatus: str) -> None:
        self.orb.set_state(state)
        self.headline.configure(text=_ellipsize(headline, 26))
        self.substatus.configure(text=_ellipsize(substatus, 40))
        if state == "WORKING":
            if not self.stop_btn.winfo_ismapped():
                self.stop_btn.pack(side="left", padx=Space.XXS, before=self.pin_btn)
        elif self.stop_btn.winfo_ismapped():
            self.stop_btn.pack_forget()

    def set_pinned(self, pinned: bool) -> None:
        self.pin_btn.configure(text_color=Color.ACCENT if pinned else Color.TXT_SECOND)


# ---------------------------------------------------------------- TaskJourney
class TaskJourney(tk.Canvas):
    """Stepped install timeline with pulsing active node — replaces PhaseProgress."""
    PHASES = ["Search", "Download", "Install", "Setup"]
    H = 58

    def __init__(self, master, **kw):
        super().__init__(master, height=self.H, bg=_canvas_bg(master),
                         highlightthickness=0, **kw)
        self._idx, self._frac, self._t = -1, 0.0, 0.0
        self.bind("<Configure>", lambda e: self._draw())
        self._tick()

    def set_phase(self, idx: int, frac: float = 1.0) -> None:
        self._idx, self._frac = idx, max(0.0, min(frac, 1.0))
        self._draw()

    def _tick(self) -> None:
        self._t += 0.06
        if 0 <= self._idx < len(self.PHASES):
            self._draw()
        self.after(60, self._tick)

    def _draw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 120)
        n, m, y = len(self.PHASES), 36, 18
        xs = [m + i * (w - 2 * m) / (n - 1) for i in range(n)]
        for i in range(n - 1):
            x0, x1 = xs[i] + 8, xs[i + 1] - 8
            if i < self._idx:
                self.create_line(x0, y, x1, y, fill=Color.ACCENT, width=2)
            elif i == self._idx:
                fx = x0 + (x1 - x0) * self._frac
                self.create_line(x0, y, x1, y, fill=Color.STROKE, width=2)
                self.create_line(x0, y, fx, y, fill=Color.ACCENT, width=2)
                sh = x0 + ((self._t * 60) % max(fx - x0, 1))
                self.create_line(sh, y, min(sh + 12, fx), y, fill=Color.ACCENT_HI, width=2)
            else:
                self.create_line(x0, y, x1, y, fill=Color.STROKE, width=2)
        for i, x in enumerate(xs):
            if i < self._idx:
                self.create_oval(x - 6, y - 6, x + 6, y + 6, fill=Color.ACCENT, outline="")
                self.create_line(x - 3, y, x - 1, y + 2, fill=Color.BG_0, width=2)
                self.create_line(x - 1, y + 2, x + 3, y - 2, fill=Color.BG_0, width=2)
            elif i == self._idx:
                r = 7 + 1.5 * math.sin(self._t * 3)
                self.create_oval(x - r, y - r, x + r, y + r, outline=Color.ACCENT, width=2)
                self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=Color.ACCENT, outline="")
            else:
                self.create_oval(x - 5, y - 5, x + 5, y + 5,
                                 outline=Color.STROKE_HI, width=2)
            self.create_text(x, y + 22, text=self.PHASES[i], font=Font.CAPTION,
                             fill=Color.TXT_PRIMARY if i <= self._idx else Color.TXT_TERT)


# ---------------------------------------------------------------- FeedCard 2.0
class FeedCard(ctk.CTkFrame):
    """Role chip + timestamp + body. Errors get friendly copy + details disclosure."""
    def __init__(self, master, role: str, text: str, *, on_retry=None, **kw):
        label, fg, chip_bg = ROLE_STYLE.get(role, ("INFO", Color.TXT_SECOND, Color.BG_3))
        is_err = role == "error"
        super().__init__(master, fg_color=Color.ERROR_BG if is_err else Color.BG_2,
                         corner_radius=Space.RADIUS_CARD, border_width=1,
                         border_color=Color.ERROR_STROKE if is_err else Color.STROKE, **kw)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=Space.M, pady=(Space.S, 0))
        ctk.CTkLabel(head, text=f"  {label}  ", font=Font.CHIP, text_color=fg,
                     fg_color=chip_bg, corner_radius=Space.RADIUS_CHIP).pack(side="left")
        ctk.CTkLabel(head, text=time.strftime("%H:%M"), font=Font.CAPTION,
                     text_color=Color.TXT_TERT).pack(side="right")

        body = humanize_error(text) if is_err else text
        self._body_lbl = ctk.CTkLabel(
            self, text=body, font=Font.BODY, text_color=Color.TXT_PRIMARY,
            wraplength=280, justify="left", anchor="w",
        )
        self._body_lbl.pack(fill="x", padx=Space.M, pady=(Space.XS, Space.S))
        self.bind("<Configure>", self._sync_wrap)
        if is_err:
            self._raw, self._open = text, False
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=Space.M, pady=(0, Space.S))
            if on_retry:
                ctk.CTkButton(row, text="Retry", width=64, height=26, command=on_retry,
                              fg_color=Color.BG_3, hover_color=Color.STROKE_HI,
                              text_color=Color.TXT_PRIMARY, font=Font.CAPTION,
                              corner_radius=8).pack(side="left", padx=(0, Space.S))
            self._dbtn = ctk.CTkButton(row, text="Show details ▸", width=100, height=26,
                                       command=self._toggle, fg_color="transparent",
                                       hover_color=Color.BG_3, text_color=Color.TXT_TERT,
                                       font=Font.CAPTION, corner_radius=8)
            self._dbtn.pack(side="left")
            self._details = ctk.CTkFrame(self, fg_color=Color.BG_0,
                                         corner_radius=Space.RADIUS_INPUT)
            self._detail_lbl = ctk.CTkLabel(
                self._details, text=self._raw[:600], font=Font.MONO,
                text_color=Color.TXT_TERT, wraplength=264, justify="left", anchor="w",
            )
            self._detail_lbl.pack(fill="x", padx=Space.S, pady=Space.S)
            ctk.CTkButton(self._details, text="Copy", width=50, height=22,
                          command=self._copy, fg_color=Color.BG_3,
                          hover_color=Color.STROKE_HI, text_color=Color.TXT_SECOND,
                          font=Font.CAPTION, corner_radius=6
                          ).pack(anchor="e", padx=Space.S, pady=(0, Space.S))

    def set_wraplength(self, width: int) -> None:
        self._body_lbl.configure(wraplength=max(120, width))
        if hasattr(self, "_detail_lbl"):
            self._detail_lbl.configure(wraplength=max(120, width - 16))

    def _sync_wrap(self, event=None) -> None:
        if event and event.width > 1:
            self.set_wraplength(event.width - Space.M * 2)

    def _toggle(self) -> None:
        self._open = not self._open
        if self._open:
            self._details.pack(fill="x", padx=Space.M, pady=(0, Space.S))
            self._dbtn.configure(text="Hide details ▾")
        else:
            self._details.pack_forget()
            self._dbtn.configure(text="Show details ▸")

    def _copy(self) -> None:
        root = self.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append(self._raw)


# ---------------------------------------------------------------- TaskGroup
class TaskGroup(ctk.CTkFrame):
    """One task run: clickable header (command + time + result dot) + collapsible cards."""
    def __init__(self, master, command_text: str, **kw):
        super().__init__(master, fg_color=Color.BG_1, corner_radius=Space.RADIUS_CARD,
                         border_width=1, border_color=Color.STROKE, **kw)
        self._open = True
        self._cards: list[FeedCard] = []
        head = ctk.CTkFrame(self, fg_color="transparent", cursor="hand2")
        head.pack(fill="x", padx=Space.M, pady=Space.S)
        self._chev = ctk.CTkLabel(head, text="▾", font=Font.CAPTION,
                                  text_color=Color.TXT_TERT, width=14)
        self._chev.pack(side="left")
        ctk.CTkLabel(head, text=command_text[:40], font=Font.TITLE,
                     text_color=Color.TXT_PRIMARY, anchor="w"
                     ).pack(side="left", padx=(Space.XS, 0), fill="x", expand=True)
        self._dot = ConnectionDot(head)
        self._dot.pack(side="right", padx=(Space.S, 0))
        ctk.CTkLabel(head, text=time.strftime("%H:%M"), font=Font.CAPTION,
                     text_color=Color.TXT_TERT).pack(side="right")
        for w in (head, *head.winfo_children()):
            w.bind("<Button-1>", lambda e: self.toggle())
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="x", padx=Space.S, pady=(0, Space.S))

    def add_card(self, role: str, text: str, *, on_retry=None) -> FeedCard:
        card = FeedCard(self.body, role, text, on_retry=on_retry)
        card.pack(fill="x", pady=(0, Space.XS))
        self._cards.append(card)
        return card

    def set_wraplength(self, width: int) -> None:
        for card in self._cards:
            card.set_wraplength(width)

    def set_result(self, ok: bool) -> None:
        self._dot.delete("all")
        self._dot.create_oval(1, 1, 7, 7,
                              fill=Color.SUCCESS if ok else Color.ERROR, outline="")

    def toggle(self) -> None:
        self._open = not self._open
        if self._open:
            self.body.pack(fill="x", padx=Space.S, pady=(0, Space.S))
        else:
            self.body.pack_forget()
        self._chev.configure(text="▾" if self._open else "▸")


# ---------------------------------------------------------------- CommandBar
class CommandBar(ctk.CTkFrame):
    """Raycast-style input: › glyph, history ↑↓, focus ring, ↵ hint."""
    def __init__(self, master, on_submit, **kw):
        super().__init__(master, fg_color=Color.BG_2, corner_radius=Space.RADIUS_INPUT,
                         border_width=1, border_color=Color.STROKE, height=40, **kw)
        self._on_submit, self._history, self._hidx = on_submit, [], 0
        ctk.CTkLabel(self, text="›", font=(Font.FAMILY, 15, "bold"),
                     text_color=Color.ACCENT, width=18).pack(side="left", padx=(Space.S, 0))
        self.entry = ctk.CTkEntry(self, placeholder_text="Ask Screeny anything…",
                                  fg_color="transparent", border_width=0, font=Font.BODY,
                                  text_color=Color.TXT_PRIMARY,
                                  placeholder_text_color=Color.TXT_TERT)
        self.entry.pack(side="left", fill="x", expand=True, padx=Space.XS, pady=4)
        ctk.CTkLabel(self, text="↵", font=Font.CAPTION,
                     text_color=Color.TXT_TERT).pack(side="right", padx=(0, Space.M))
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<Up>", self._prev)
        self.entry.bind("<Down>", self._next)
        self.entry.bind("<FocusIn>", lambda e: self.configure(border_color=Color.ACCENT_DIM))
        self.entry.bind("<FocusOut>", lambda e: self.configure(border_color=Color.STROKE))

    def _submit(self, _=None) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self._history.append(text)
        self._hidx = len(self._history)
        self.entry.delete(0, "end")
        self._on_submit(text)

    def _prev(self, _=None) -> None:
        if self._history and self._hidx > 0:
            self._hidx -= 1
            self.entry.delete(0, "end")
            self.entry.insert(0, self._history[self._hidx])

    def _next(self, _=None) -> None:
        if self._hidx < len(self._history) - 1:
            self._hidx += 1
            self.entry.delete(0, "end")
            self.entry.insert(0, self._history[self._hidx])
        else:
            self._hidx = len(self._history)
            self.entry.delete(0, "end")


# ---------------------------------------------------------------- EmptyState
class EmptyState(ctk.CTkFrame):
    def __init__(self, master, on_example, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        orb = tk.Canvas(self, width=40, height=40, bg=_canvas_bg(self),
                        highlightthickness=0)
        orb.create_oval(12, 12, 28, 28, outline=Color.ACCENT_DIM, width=2)
        orb.create_oval(6, 6, 34, 34, outline=Color.STROKE, width=1)
        orb.pack(pady=(Space.XL, Space.S))
        ctk.CTkLabel(self, text="Screeny is listening", font=Font.TITLE,
                     text_color=Color.TXT_SECOND).pack()
        ctk.CTkLabel(self, text="Say \u201cHey Screeny\u201d or type a command",
                     font=Font.CAPTION, text_color=Color.TXT_TERT).pack(pady=(0, Space.M))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack()
        for ex in ("install discord", "open spotify", "search the weather"):
            ctk.CTkButton(row, text=ex, height=26, font=Font.CAPTION,
                          fg_color=Color.BG_2, hover_color=Color.BG_3,
                          text_color=Color.TXT_SECOND, corner_radius=Space.RADIUS_CHIP,
                          border_width=1, border_color=Color.STROKE,
                          command=lambda t=ex: on_example(t)
                          ).pack(side="left", padx=Space.XS)


# ---------------------------------------------------------------- SettingsDrawer (stub)
class SettingsDrawer(ctk.CTkFrame):
    def __init__(self, master, *, info: dict, on_open_logs, on_mic_toggle=None, **kw):
        super().__init__(master, fg_color=Color.BG_1, corner_radius=Space.RADIUS_CARD,
                         border_width=1, border_color=Color.STROKE, **kw)
        self._on_mic_toggle = on_mic_toggle

        def row(label, value_widget_or_text):
            r = ctk.CTkFrame(self, fg_color="transparent")
            r.pack(fill="x", padx=Space.M, pady=Space.XS)
            ctk.CTkLabel(r, text=label, font=Font.CAPTION,
                         text_color=Color.TXT_TERT, width=90, anchor="w").pack(side="left")
            if isinstance(value_widget_or_text, str):
                ctk.CTkLabel(r, text=value_widget_or_text, font=Font.BODY,
                             text_color=Color.TXT_PRIMARY, anchor="w").pack(side="left")
            return r

        ctk.CTkLabel(self, text="Settings", font=Font.TITLE,
                     text_color=Color.TXT_PRIMARY).pack(anchor="w",
                     padx=Space.M, pady=(Space.S, Space.XS))
        conn = ctk.CTkFrame(self, fg_color="transparent")
        conn.pack(fill="x", padx=Space.M, pady=Space.XS)
        ctk.CTkLabel(conn, text="Ollama", font=Font.CAPTION, text_color=Color.TXT_TERT,
                     width=90, anchor="w").pack(side="left")
        self.conn_dot = ConnectionDot(conn)
        self.conn_dot.pack(side="left")
        self.conn_lbl = ctk.CTkLabel(conn, text="checking…", font=Font.BODY,
                                     text_color=Color.TXT_PRIMARY)
        self.conn_lbl.pack(side="left", padx=Space.XS)
        row("Vision", info.get("vision_model", "qwen2.5vl:7b"))
        row("Planner", info.get("planner_model", "llama3.2:3b"))
        row("Voice", info.get("voice", "Whisper + TTS"))
        if on_mic_toggle:
            ctk.CTkButton(self, text="Toggle microphone mute", height=28, font=Font.CAPTION,
                          fg_color=Color.BG_3, hover_color=Color.STROKE_HI,
                          text_color=Color.TXT_PRIMARY, corner_radius=8,
                          command=on_mic_toggle).pack(anchor="w", padx=Space.M, pady=Space.XS)
        ctk.CTkButton(self, text="Open log folder", height=28, font=Font.CAPTION,
                      fg_color=Color.BG_3, hover_color=Color.STROKE_HI,
                      text_color=Color.TXT_PRIMARY, corner_radius=8,
                      command=on_open_logs).pack(anchor="w", padx=Space.M,
                                                 pady=(Space.S, Space.M))

    def set_connection(self, ok: bool) -> None:
        self.conn_dot.set_online(ok)
        self.conn_lbl.configure(text="connected" if ok else "not running",
                                text_color=Color.SUCCESS if ok else Color.ERROR)
