"""Validation and execution of browser actions proposed by an agent or operator.

The model only *proposes*; the application validates every action against a
narrow policy before executing it. Page content is untrusted input and can
never widen this policy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Keys a worker may press. No modifiers (prevents Ctrl+L / Cmd+S / devtools etc.)
# and no Enter/Tab (prevents form submission and focus games).
KEY_ALLOWLIST = {
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "Escape",
    "PageUp",
    "PageDown",
    "Home",
    "End",
    "+",
    "-",
    "=",
}

_KEY_MAP = {
    "ENTER": "Enter",
    "RETURN": "Enter",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "TAB": "Tab",
    "SPACE": "Space",
    "BACKSPACE": "Backspace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "PageUp",
    "PAGEDOWN": "PageDown",
    "UP": "ArrowUp",
    "DOWN": "ArrowDown",
    "LEFT": "ArrowLeft",
    "RIGHT": "ArrowRight",
    "ARROWUP": "ArrowUp",
    "ARROWDOWN": "ArrowDown",
    "ARROWLEFT": "ArrowLeft",
    "ARROWRIGHT": "ArrowRight",
    "CTRL": "Control",
    "CONTROL": "Control",
    "SHIFT": "Shift",
    "OPTION": "Alt",
    "ALT": "Alt",
    "META": "Meta",
    "CMD": "Meta",
    "COMMAND": "Meta",
}


def normalize_key(key: str) -> str:
    """Map model-emitted key names to Playwright names (per OpenAI integration recipes)."""
    return _KEY_MAP.get(key.upper(), key) if isinstance(key, str) else key


_BUTTONS = {"left": "left", "right": "right", "wheel": "middle"}


def _get(a: Any, k: str, default=None):
    if isinstance(a, dict):
        return a.get(k, default)
    return getattr(a, k, default)


@dataclass
class ActionPolicy:
    viewport_w: int
    viewport_h: int
    allow_typing: bool = False
    allow_right_click: bool = False
    max_scroll: int = 2000
    max_drag_points: int = 30
    max_wait_s: float = 2.0
    keys: set[str] = field(default_factory=lambda: set(KEY_ALLOWLIST))


@dataclass
class Validated:
    ok: bool
    reason: str
    action: dict


def validate_action(a: Any, policy: ActionPolicy) -> Validated:
    t = _get(a, "type")
    raw = a if isinstance(a, dict) else (a.model_dump() if hasattr(a, "model_dump") else {"type": t})

    def inside(x, y) -> bool:
        try:
            return 0 <= float(x) < policy.viewport_w and 0 <= float(y) < policy.viewport_h
        except (TypeError, ValueError):
            return False

    if _get(a, "keys") and t != "keypress":
        return Validated(False, "modifier keys on mouse actions are not permitted", raw)
    if t in ("click", "double_click", "move"):
        x, y = _get(a, "x"), _get(a, "y")
        if not inside(x, y):
            return Validated(False, f"{t} outside viewport ({x},{y})", raw)
        btn = _get(a, "button", "left") or "left"
        if t == "click":
            if btn not in _BUTTONS:
                return Validated(False, f"button '{btn}' not supported", raw)
            if btn == "right" and not policy.allow_right_click:
                return Validated(False, "right click not permitted", raw)
        return Validated(True, "ok", {"type": t, "x": int(float(x)), "y": int(float(y)), "button": _BUTTONS.get(btn, "left")})
    if t == "drag":
        path = _get(a, "path") or []
        pts = []
        for p in path:
            px, py = (_get(p, "x"), _get(p, "y")) if not isinstance(p, (list, tuple)) else (p[0], p[1])
            if not inside(px, py):
                return Validated(False, "drag path leaves viewport", raw)
            pts.append((int(float(px)), int(float(py))))
        if len(pts) < 2 or len(pts) > policy.max_drag_points:
            return Validated(False, "drag path must have 2..%d points" % policy.max_drag_points, raw)
        return Validated(True, "ok", {"type": "drag", "path": pts})
    if t == "scroll":
        x, y = _get(a, "x", 0), _get(a, "y", 0)
        sx, sy = _get(a, "scroll_x", 0) or 0, _get(a, "scroll_y", 0) or 0
        if not inside(x, y):
            return Validated(False, "scroll origin outside viewport", raw)
        if abs(sx) > policy.max_scroll or abs(sy) > policy.max_scroll:
            return Validated(False, "scroll too large", raw)
        return Validated(True, "ok", {"type": "scroll", "x": int(x), "y": int(y), "scroll_x": int(sx), "scroll_y": int(sy)})
    if t == "keypress":
        keys = [normalize_key(k) for k in (_get(a, "keys") or [])]
        if not keys:
            return Validated(False, "empty keypress", raw)
        bad = [k for k in keys if k not in policy.keys]
        if bad or len(keys) > 1:
            return Validated(False, f"keys not permitted: {keys}", raw)
        return Validated(True, "ok", {"type": "keypress", "keys": keys})
    if t == "type":
        if not policy.allow_typing:
            return Validated(False, "typing text is not permitted for this task", raw)
        text = str(_get(a, "text", ""))
        return Validated(True, "ok", {"type": "type", "text": text[:200]})
    if t == "wait":
        return Validated(True, "ok", {"type": "wait"})
    if t == "screenshot":
        return Validated(True, "ok", {"type": "screenshot"})
    return Validated(False, f"unsupported action type {t!r}", raw)


async def execute_action(page, act: dict, policy: ActionPolicy) -> None:
    t = act["type"]
    if t == "click":
        await page.mouse.click(act["x"], act["y"], button=act.get("button", "left"))
    elif t == "double_click":
        await page.mouse.dblclick(act["x"], act["y"])
    elif t == "move":
        await page.mouse.move(act["x"], act["y"])
    elif t == "drag":
        (sx, sy), rest = act["path"][0], act["path"][1:]
        await page.mouse.move(sx, sy)
        await page.mouse.down()
        for x, y in rest:
            await page.mouse.move(x, y, steps=4)
        await page.mouse.up()
    elif t == "scroll":
        await page.mouse.move(act["x"], act["y"])
        await page.mouse.wheel(act["scroll_x"], act["scroll_y"])
    elif t == "keypress":
        await page.keyboard.press("+".join(act["keys"]))
    elif t == "type":
        await page.keyboard.type(act["text"])
    elif t == "wait":
        await asyncio.sleep(policy.max_wait_s)
    elif t == "screenshot":
        return
    await asyncio.sleep(0.35)
