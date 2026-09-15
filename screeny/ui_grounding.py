from __future__ import annotations

import time
from dataclasses import dataclass

# Control types that are worth clicking / interacting with.
CLICKABLE_TYPES = {
    "ButtonControl",
    "HyperlinkControl",
    "ListItemControl",
    "MenuItemControl",
    "MenuItem",
    "TabItemControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ToggleSwitchControl",
    "SplitButtonControl",
    "ComboBoxControl",
    "EditControl",
    "TreeItemControl",
    "ImageControl",
}

# Friendly names for the prompt.
TYPE_LABELS = {
    "ButtonControl": "button",
    "HyperlinkControl": "link",
    "ListItemControl": "item",
    "MenuItemControl": "menu",
    "MenuItem": "menu",
    "TabItemControl": "tab",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
    "ToggleSwitchControl": "toggle",
    "SplitButtonControl": "button",
    "ComboBoxControl": "dropdown",
    "EditControl": "textbox",
    "TreeItemControl": "item",
    "ImageControl": "image",
}


@dataclass
class UIElement:
    name: str
    kind: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> int:
        return (self.left + self.right) // 2

    @property
    def cy(self) -> int:
        return (self.top + self.bottom) // 2

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def is_taskbar_zone(x: int, y: int, screen_width: int, screen_height: int) -> bool:
    """True when coords sit in the Windows taskbar / notification area."""
    if y >= screen_height * 0.9:
        return True
    if x >= screen_width * 0.82 and y >= screen_height * 0.82:
        return True
    return False


def click_point_for_element(
    el: UIElement, screen_width: int, screen_height: int
) -> tuple[int, int]:
    """Return click coords; fix UIA bboxes whose center falls in the taskbar strip."""
    cx, cy = el.cx, el.cy
    if not is_taskbar_zone(cx, cy, screen_width, screen_height):
        return cx, cy
    # Custom web buttons often expose a huge container — click the visible top edge.
    if el.height > 48 and el.top < screen_height * 0.92:
        target_y = el.top + max(min(el.height // 4, 36), 12)
        target_y = min(target_y, int(screen_height * 0.86))
        return cx, target_y
    return cx, cy


_CAPTION_BUTTONS = {"minimize", "maximize", "restore", "restore down"}

# Browser/window chrome controls that are almost never the intended content
# target. Clicking these (e.g. Forward) navigates away and breaks the task.
# Only filtered when they sit in the top toolbar strip, so legitimate
# "Back"/"Next" wizard buttons lower on screen are kept.
_NAV_BUTTONS = {
    "back",
    "forward",
    "reload",
    "refresh",
    "reload this page",
    "refresh this page",
    "stop loading this page",
    "home",
    "view site information",
    "search or type url",
    "bookmark this tab",
}


def _is_window_caption_button(name: str, left: int, top: int, screen_width: int) -> bool:
    low = name.lower().strip()
    if low in _CAPTION_BUTTONS:
        return True
    # "Close" is only a caption button when it's the small top-right window X.
    if low == "close" and top < 60 and left > screen_width * 0.82:
        return True
    # Browser navigation buttons live in the top toolbar strip.
    if low in _NAV_BUTTONS and top < 130:
        return True
    return False


def grounding_available() -> bool:
    try:
        import uiautomation  # noqa: F401

        return True
    except Exception:
        return False


def count_clickable_elements(
    screen_width: int,
    screen_height: int,
    *,
    time_budget: float = 0.8,
    min_count: int = 4,
) -> int:
    """Quick probe: enough accessible controls to try UIA-first routing?"""
    elements = get_clickable_elements(
        screen_width,
        screen_height,
        time_budget=time_budget,
        max_elements=max(min_count + 4, 12),
        foreground_first=True,
    )
    return len(elements)


def get_clickable_elements(
    screen_width: int,
    screen_height: int,
    *,
    time_budget: float = 3.5,
    max_nodes: int = 2500,
    max_elements: int = 48,
    foreground_first: bool = False,
    foreground_only: bool = False,
) -> list[UIElement]:
    """Scan visible top-level windows (except Screeny) for clickable elements.

    Coordinates come back in physical screen pixels, matching pyautogui.
    When foreground_only is True, only the foreground window is scanned —
    use this during installer wizards so Chrome elements are not offered.
    Returns [] if UI Automation isn't available or finds nothing in time.
    """
    try:
        import uiautomation as auto
    except Exception:
        return []

    deadline = time.time() + time_budget
    nodes = [0]
    found: list[UIElement] = []
    seen: set[tuple[int, int, int, int]] = set()

    def consider(ctrl) -> None:
        try:
            if ctrl.ControlTypeName not in CLICKABLE_TYPES:
                return
            if ctrl.IsOffscreen:
                return
            rect = ctrl.BoundingRectangle
            if rect is None:
                return
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
            w, h = right - left, bottom - top
            if w < 8 or h < 8 or w > screen_width or h > screen_height:
                return
            # Must be on the visible screen.
            if right <= 0 or bottom <= 0 or left >= screen_width or top >= screen_height:
                return
            name = (ctrl.Name or "").strip()
            kind = ctrl.ControlTypeName
            # Skip nameless non-edit controls (rarely useful, very noisy).
            if not name and kind not in {"EditControl", "ComboBoxControl"}:
                return
            # Skip window caption buttons — clicking these (esp. Maximize) is
            # almost always a misfire that resizes/closes the window. The agent
            # uses hotkeys for window/tab control instead.
            if _is_window_caption_button(name, left, top, screen_width):
                return
            key = (left, top, right, bottom)
            if key in seen:
                return
            seen.add(key)
            el = UIElement(
                name=name[:80],
                kind=TYPE_LABELS.get(kind, "control"),
                left=max(0, left),
                top=max(0, top),
                right=min(screen_width, right),
                bottom=min(screen_height, bottom),
            )
            # Drop controls whose center is in the taskbar — UIA often misreports web CTAs.
            if is_taskbar_zone(el.cx, el.cy, screen_width, screen_height):
                if el.top >= screen_height * 0.78:
                    return
            found.append(el)
        except Exception:
            return

    def walk(ctrl, depth: int) -> None:
        if depth > 22 or nodes[0] > max_nodes or time.time() > deadline:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            nodes[0] += 1
            if nodes[0] > max_nodes or time.time() > deadline:
                return
            consider(child)
            walk(child, depth + 1)

    windows: list = []
    if foreground_first:
        try:
            fg = auto.GetForegroundControl()
            if fg is not None:
                windows.append(fg)
        except Exception:
            pass

    if not foreground_only:
        try:
            root = auto.GetRootControl()
            for win in root.GetChildren():
                if win not in windows:
                    windows.append(win)
        except Exception:
            if not windows:
                return []
    elif not windows:
        return []

    for win in windows:
        if time.time() > deadline or len(found) >= max_elements:
            break
        try:
            name = (win.Name or "")
            if "screeny" in name.lower():
                continue
            if win.IsOffscreen:
                continue
            rect = win.BoundingRectangle
            if rect is None or rect.width() < 200 or rect.height() < 120:
                continue
        except Exception:
            continue
        walk(win, 0)

    return found[:max_elements]


def interact_element(el: UIElement) -> tuple[bool, str]:
    """Activate a control via UIA patterns when possible (PyWinAssistant-style).

    Falls back to a center click. Returns (ok, detail).
    """
    ctrl = _find_control_for_element(el)
    if ctrl is not None:
        try:
            invoke = ctrl.GetInvokePattern()
            if invoke is not None:
                invoke.Invoke()
                label = el.name or el.kind
                return True, f"Activated {label}."
        except Exception:
            pass
        try:
            toggle = ctrl.GetTogglePattern()
            if toggle is not None:
                toggle.Toggle()
                label = el.name or el.kind
                return True, f"Toggled {label}."
        except Exception:
            pass
        try:
            selection = ctrl.GetSelectionItemPattern()
            if selection is not None:
                selection.Select()
                label = el.name or el.kind
                return True, f"Selected {label}."
        except Exception:
            pass
        try:
            expand = ctrl.GetExpandCollapsePattern()
            if expand is not None:
                expand.Expand()
                label = el.name or el.kind
                return True, f"Expanded {label}."
        except Exception:
            pass

    try:
        import pyautogui

        from screeny.config import SETTINGS
        from screeny.cursor_anim import animate_move, human_click

        sw = pyautogui.size().width
        sh = pyautogui.size().height
        cx, cy = click_point_for_element(el, sw, sh)
        if SETTINGS.cursor_animate:
            human_click(cx, cy)
        else:
            pyautogui.click(cx, cy)
        label = el.name or el.kind
        return True, f"Clicked {label} @ ({cx},{cy})."
    except Exception as exc:
        return False, str(exc)


def _find_control_for_element(el: UIElement, tolerance: int = 6):
    try:
        import uiautomation as auto
    except Exception:
        return None

    target = (
        el.left,
        el.top,
        el.right,
        el.bottom,
        el.name.lower().strip(),
    )

    def matches(ctrl) -> bool:
        try:
            if ctrl.ControlTypeName not in CLICKABLE_TYPES:
                return False
            rect = ctrl.BoundingRectangle
            if rect is None:
                return False
            if (
                abs(rect.left - target[0]) > tolerance
                or abs(rect.top - target[1]) > tolerance
                or abs(rect.right - target[2]) > tolerance
                or abs(rect.bottom - target[3]) > tolerance
            ):
                return False
            name = (ctrl.Name or "").strip().lower()
            if target[4] and name != target[4]:
                return False
            return True
        except Exception:
            return False

    def walk(root, depth: int = 0):
        if depth > 22:
            return None
        try:
            children = root.GetChildren()
        except Exception:
            return None
        for child in children:
            if matches(child):
                return child
            hit = walk(child, depth + 1)
            if hit is not None:
                return hit
        return None

    try:
        fg = auto.GetForegroundControl()
        if fg is not None:
            hit = walk(fg, 0)
            if hit is not None:
                return hit
    except Exception:
        pass

    try:
        for win in auto.GetRootControl().GetChildren():
            hit = walk(win, 0)
            if hit is not None:
                return hit
    except Exception:
        pass
    return None


def format_elements(elements: list[UIElement]) -> str:
    if not elements:
        return "(none detected — fall back to x_norm/y_norm from the screenshot)"
    lines = []
    for i, el in enumerate(elements, 1):
        label = f'"{el.name}"' if el.name else "(no label)"
        lines.append(f"[{i}] {el.kind} {label}")
    return "\n".join(lines)


def pick_by_label(elements: list[UIElement], label: object) -> UIElement | None:
    """Resolve by list index (1-based) or by element name text."""
    if not elements or label is None:
        return None
    raw = str(label).strip()
    if not raw:
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(elements):
            return elements[idx - 1]
    except (TypeError, ValueError):
        pass
    # Small models often send the element caption instead of its number.
    hit = best_match(elements, raw)
    if hit is not None:
        return hit
    low = raw.lower()
    for el in elements:
        if el.name and (el.name.lower() in low or low in el.name.lower()):
            return el
    return None


def best_match(
    elements: list[UIElement],
    target: str,
    hint: tuple[int, int] | None = None,
) -> UIElement | None:
    """Heuristic fallback: match a target description to an element by name
    similarity, optionally biased toward the model's coarse guess location."""
    import difflib

    target = (target or "").strip().lower()
    if not elements:
        return None

    target_tokens = {t for t in _tokens(target) if len(t) > 2}

    best: UIElement | None = None
    best_score = 0.0
    for el in elements:
        name = el.name.lower()
        if not name:
            score = 0.0
        else:
            ratio = difflib.SequenceMatcher(None, target, name).ratio()
            name_tokens = set(_tokens(name))
            overlap = (
                len(target_tokens & name_tokens) / len(target_tokens)
                if target_tokens
                else 0.0
            )
            substring = 1.0 if (name in target or target in name) and name else 0.0
            score = max(ratio, overlap, substring * 0.95)

        # Proximity is only a small additive tiebreak, never a penalty: a strong
        # text match must win even if the model's coarse guess was way off.
        if hint is not None and score > 0:
            dist = ((el.cx - hint[0]) ** 2 + (el.cy - hint[1]) ** 2) ** 0.5
            score += max(0.0, 1.0 - dist / 1200.0) * 0.1

        if score > best_score:
            best_score = score
            best = el

    if best_score >= 0.45:
        return best
    return None


def _tokens(text: str) -> list[str]:
    import re

    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


MAX_UIA_AREA_FRAC = 0.08
_BROWSER_CHROME_INSET = 110


def browser_viewport_rect(
    screen_width: int, screen_height: int
) -> tuple[int, int, int, int] | None:
    """Web content area inside the foreground browser window (left, top, right, bottom)."""
    try:
        import uiautomation as auto
    except Exception:
        return None

    try:
        fg = auto.GetForegroundControl()
        if fg is None:
            return None
        doc = fg.DocumentControl(searchDepth=16)
        if doc.Exists(maxSearchSeconds=0.4):
            r = doc.BoundingRectangle
            if r and r.width() > 200 and r.height() > 200:
                return (
                    max(0, r.left),
                    max(0, r.top),
                    min(screen_width, r.right),
                    min(screen_height, r.bottom),
                )
        wr = fg.BoundingRectangle
        if wr and wr.width() > 200:
            top = wr.top + _BROWSER_CHROME_INSET
            return (
                max(0, wr.left),
                top,
                min(screen_width, wr.right),
                min(screen_height, wr.bottom),
            )
    except Exception:
        return None
    return None


def page_signature() -> str:
    """Cheap navigation fingerprint: browser Document control name."""
    try:
        import uiautomation as auto
    except Exception:
        return ""

    try:
        fg = auto.GetForegroundControl()
        if fg is None:
            return ""
        doc = fg.DocumentControl(searchDepth=16)
        if doc.Exists(maxSearchSeconds=0.3):
            return (doc.Name or "")[:120]
    except Exception:
        pass
    return ""


def wait_for_page_settle(
    prev_sig: str,
    *,
    timeout: float = 8.0,
    stable_for: float = 0.8,
) -> str:
    """Block until the page signature differs from prev_sig AND holds steady."""
    t0 = time.time()
    last = page_signature()
    last_t = time.time()
    while time.time() - t0 < timeout:
        cur = page_signature()
        if cur != last:
            last, last_t = cur, time.time()
        elif cur and cur != prev_sig and time.time() - last_t >= stable_for:
            return cur
        time.sleep(0.25)
    return last


def viewport_as_ltrb(viewport: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = viewport
    return left, top, right, bottom


def in_viewport(
    x: int,
    y: int,
    viewport: tuple[int, int, int, int],
    *,
    margin: int = 4,
) -> bool:
    left, top, right, bottom = viewport_as_ltrb(viewport)
    return left + margin <= x <= right - margin and top + margin <= y <= bottom - margin


def element_in_viewport(
    el: UIElement, viewport: tuple[int, int, int, int]
) -> bool:
    left, top, right, bottom = viewport_as_ltrb(viewport)
    cx, cy = el.cx, el.cy
    return left <= cx <= right and top <= cy <= bottom


def uia_element_area_ok(
    el: UIElement, viewport: tuple[int, int, int, int]
) -> bool:
    left, top, right, bottom = viewport_as_ltrb(viewport)
    vw = max(1, right - left)
    vh = max(1, bottom - top)
    area = el.width * el.height
    return area <= MAX_UIA_AREA_FRAC * vw * vh


def filter_elements_for_browser(
    elements: list[UIElement],
    viewport: tuple[int, int, int, int] | None,
    *,
    screen_width: int,
    screen_height: int,
) -> list[UIElement]:
    """Drop browser chrome and oversized UIA containers before the LLM sees them."""
    if viewport is None:
        viewport = browser_viewport_rect(screen_width, screen_height)
    if viewport is None:
        return elements

    kept: list[UIElement] = []
    for el in elements:
        if not element_in_viewport(el, viewport):
            continue
        if not uia_element_area_ok(el, viewport):
            continue
        kept.append(el)
    return kept if kept else elements
