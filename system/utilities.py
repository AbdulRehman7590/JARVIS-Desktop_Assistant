"""Miscellaneous helper actions exposed as JARVIS commands.

Kept in one place so the executor stays clean. Each function returns a
small dataclass with ``ok`` + ``message`` (and optionally a ``payload``)
so the executor can speak the message and log the outcome uniformly.
"""
from __future__ import annotations

import ast
import operator as op
import os
import platform
import random
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from utils.logger import get_logger
from utils.paths import APP_ROOT, DATA_DIR, ensure_dirs

_log = get_logger()


@dataclass
class UtilResult:
    ok: bool
    message: str
    payload: Optional[Any] = None


# ---------------------------------------------------------------------------
# Time / date
# ---------------------------------------------------------------------------
def get_time() -> UtilResult:
    now = datetime.now()
    return UtilResult(True, f"It's {now.strftime('%I:%M %p').lstrip('0')}.")


def get_date() -> UtilResult:
    now = datetime.now()
    return UtilResult(True, f"Today is {now.strftime('%A, %B %d, %Y')}.")


# ---------------------------------------------------------------------------
# Jokes — built-in list, no API call. Apologies in advance.
# ---------------------------------------------------------------------------
_JOKES = (
    "Why did the developer go broke? Because he used up all his cache.",
    "I told my computer I needed a break, and it said 'No problem — I'll go to sleep.'",
    "There are 10 kinds of people in the world: those who understand binary and those who don't.",
    "Why do Java developers wear glasses? Because they don't C#.",
    "A SQL query walks into a bar, walks up to two tables and asks: 'Can I join you?'",
    "Why did the AI cross the road? To optimise the chicken.",
    "Debugging is like being the detective in a crime movie where you are also the murderer.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why did the programmer quit his job? He didn't get arrays.",
    "Knock knock. Who's there? Recursion. Recursion who? Knock knock.",
)


def tell_joke() -> UtilResult:
    return UtilResult(True, random.choice(_JOKES))


# ---------------------------------------------------------------------------
# Calculator — safe AST-based evaluator (no eval(), no builtins).
# ---------------------------------------------------------------------------
_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod, ast.Pow: op.pow,
    ast.USub: op.neg, ast.UAdd: op.pos,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError(f"Unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _OPS:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _OPS:
            raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"Unsupported expression: {ast.dump(node)}")


def calculate(expression: str) -> UtilResult:
    expr = (expression or "").strip()
    if not expr:
        return UtilResult(False, "Tell me what to calculate.")
    if len(expr) > 200:
        return UtilResult(False, "That expression is a bit long for me.")
    try:
        tree = ast.parse(expr, mode="eval")
        result = _eval_node(tree)
    except (SyntaxError, ValueError, ZeroDivisionError) as exc:
        return UtilResult(False, f"I couldn't evaluate that: {exc}")

    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return UtilResult(True, f"{expr} equals {result}.", payload=result)


# ---------------------------------------------------------------------------
# System info (CPU / memory / OS)
# ---------------------------------------------------------------------------
def system_info() -> UtilResult:
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return UtilResult(False, "I need the psutil package for system info.")

    try:
        cpu = psutil.cpu_percent(interval=0.4)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(str(APP_ROOT.anchor or APP_ROOT))
    except Exception as exc:  # noqa: BLE001
        return UtilResult(False, f"Couldn't read system info: {exc}")

    msg = (
        f"{platform.system()} {platform.release()}, "
        f"{platform.python_implementation()} {platform.python_version()}. "
        f"CPU at {cpu:.0f} percent. "
        f"RAM {mem.percent:.0f} percent of {mem.total // (1024**3)} gigs. "
        f"Drive {APP_ROOT.anchor.rstrip(os.sep) or '/'} is {disk.percent:.0f} percent full."
    )
    return UtilResult(True, msg)


# ---------------------------------------------------------------------------
# Screenshot — saved to data/screenshots/
# ---------------------------------------------------------------------------
def screenshot() -> UtilResult:
    ensure_dirs()
    out_dir = DATA_DIR / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    out_path = out_dir / fname

    try:
        from PIL import ImageGrab  # noqa: PLC0415
    except ImportError:
        return UtilResult(False, "Pillow isn't installed — I can't capture screenshots.")

    try:
        img = ImageGrab.grab(all_screens=True)
        img.save(out_path)
    except OSError as exc:
        return UtilResult(False, f"Screenshot failed: {exc}")

    return UtilResult(True, f"Screenshot saved as {fname}.", payload=out_path)


# ---------------------------------------------------------------------------
# Volume control (Windows only — uses keyboard scan codes via PowerShell).
# ---------------------------------------------------------------------------
def _send_media_key(vk_hex: str, presses: int = 1) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes  # noqa: PLC0415

        user32 = ctypes.windll.user32
        vk = int(vk_hex, 16)
        for _ in range(presses):
            user32.keybd_event(vk, 0, 0, 0)         # keydown
            user32.keybd_event(vk, 0, 2, 0)         # keyup
        return True
    except Exception as exc:  # noqa: BLE001
        _log.error("send_media_key failed: %s", exc)
        return False


def volume_up(steps: int = 4) -> UtilResult:
    ok = _send_media_key("0xAF", steps)  # VK_VOLUME_UP
    return UtilResult(ok, f"Volume up by {steps} steps." if ok else "Couldn't change volume.")


def volume_down(steps: int = 4) -> UtilResult:
    ok = _send_media_key("0xAE", steps)  # VK_VOLUME_DOWN
    return UtilResult(ok, f"Volume down by {steps} steps." if ok else "Couldn't change volume.")


def volume_mute() -> UtilResult:
    ok = _send_media_key("0xAD", 1)      # VK_VOLUME_MUTE
    return UtilResult(ok, "Toggled mute." if ok else "Couldn't toggle mute.")


# ---------------------------------------------------------------------------
# Open URL — only http(s) and only with a sensible-looking host.
# ---------------------------------------------------------------------------
def open_url(url: str) -> UtilResult:
    if not url or not url.strip():
        return UtilResult(False, "Tell me which URL to open.")
    text = url.strip().strip('"').strip("'")

    # Accept "google.com" → "https://google.com".
    if "://" not in text:
        text = "https://" + text

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return UtilResult(False, "I only open http or https links.")
    host = (parsed.netloc or "").split(":")[0]
    if not host or "." not in host:
        return UtilResult(False, f"That doesn't look like a real address: {url}")

    # webbrowser handles all platforms and uses the user's default browser.
    try:
        import webbrowser  # noqa: PLC0415

        webbrowser.open(text, new=2, autoraise=True)
    except Exception as exc:  # noqa: BLE001
        return UtilResult(False, f"Couldn't open that link: {exc}")

    return UtilResult(True, f"Opening {host}.")


def web_search(query: str) -> UtilResult:
    """Open a Google search for ``query``."""
    q = (query or "").strip()
    if not q:
        return UtilResult(False, "Tell me what to search for.")
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)
    return open_url(url)
