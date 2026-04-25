"""Whitelist-based application launcher.

Why a whitelist?
    Letting a voice assistant run *arbitrary* executables is a recipe for
    disaster. We instead resolve a friendly name (e.g. ``"chrome"``) to a
    known-safe argv list using a curated mapping. Unknown names are refused.

The mapping is generous about common applications and uses ``shutil.which``
+ a list of probable install paths so the assistant works on most Windows
machines without configuration. Users can extend the whitelist via
``data/extra_apps.json`` (added at runtime if present).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger
from utils.paths import DATA_DIR

_log = get_logger()


@dataclass
class AppLaunchResult:
    ok: bool
    message: str


def _first_existing(*candidates: str) -> Optional[str]:
    """Return the first path from ``candidates`` that exists, or ``None``."""
    for c in candidates:
        if not c:
            continue
        if Path(c).exists():
            return c
    return None


def _which(*names: str) -> Optional[str]:
    for n in names:
        path = shutil.which(n)
        if path:
            return path
    return None


def _expand(path: str) -> str:
    return os.path.expandvars(path)


# ---------------------------------------------------------------------------
# Built-in whitelist
# ---------------------------------------------------------------------------
def _default_whitelist() -> Dict[str, List[str]]:
    """Map friendly names → argv lists for common Windows applications."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")

    chrome = _first_existing(
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ) or _which("chrome")

    edge = _first_existing(
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
    ) or _which("msedge")

    firefox = _first_existing(
        os.path.join(pf, "Mozilla Firefox", "firefox.exe"),
        os.path.join(pf86, "Mozilla Firefox", "firefox.exe"),
    ) or _which("firefox")

    code = _first_existing(
        os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe"),
        os.path.join(pf, "Microsoft VS Code", "Code.exe"),
    ) or _which("code")

    cursor = _first_existing(
        os.path.join(local, "Programs", "cursor", "Cursor.exe"),
    ) or _which("cursor")

    spotify = _first_existing(
        os.path.join(local, "Microsoft", "WindowsApps", "Spotify.exe"),
        os.path.join(local, "Spotify", "Spotify.exe"),
    ) or _which("spotify")

    mapping: Dict[str, List[str]] = {
        "notepad": ["notepad.exe"],
        "calculator": ["calc.exe"],
        "calc": ["calc.exe"],
        "paint": ["mspaint.exe"],
        "wordpad": ["write.exe"],
        "file explorer": ["explorer.exe"],
        "explorer": ["explorer.exe"],
        "task manager": ["taskmgr.exe"],
        "control panel": ["control.exe"],
        "cmd": ["cmd.exe"],
        "command prompt": ["cmd.exe"],
        "powershell": ["powershell.exe"],
        "snipping tool": ["snippingtool.exe"],
    }
    if chrome:
        mapping["chrome"] = [chrome]
        mapping["google chrome"] = [chrome]
    if edge:
        mapping["edge"] = [edge]
        mapping["microsoft edge"] = [edge]
    if firefox:
        mapping["firefox"] = [firefox]
    if code:
        mapping["vscode"] = [code]
        mapping["vs code"] = [code]
        mapping["visual studio code"] = [code]
    if cursor:
        mapping["cursor"] = [cursor]
    if spotify:
        mapping["spotify"] = [spotify]

    return mapping


def _load_user_whitelist() -> Dict[str, List[str]]:
    """Merge in optional ``data/extra_apps.json`` if present."""
    extra_file = DATA_DIR / "extra_apps.json"
    if not extra_file.exists():
        return {}
    try:
        with extra_file.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        clean: Dict[str, List[str]] = {}
        for name, value in raw.items():
            if not isinstance(name, str):
                continue
            if isinstance(value, str):
                clean[name.lower()] = [_expand(value)]
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                clean[name.lower()] = [_expand(v) for v in value]
        _log.info("Loaded %d user-defined apps.", len(clean))
        return clean
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("Could not load extra_apps.json: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# AppLauncher
# ---------------------------------------------------------------------------
class AppLauncher:
    """Resolve a friendly app name and launch it via ``subprocess.Popen``."""

    def __init__(self) -> None:
        self._apps: Dict[str, List[str]] = _default_whitelist()
        self._apps.update(_load_user_whitelist())
        _log.info("App whitelist contains %d entries.", len(self._apps))

    def known_apps(self) -> List[str]:
        return sorted(self._apps.keys())

    def launch(self, name: str) -> AppLaunchResult:
        if not name:
            return AppLaunchResult(False, "Please tell me which app to launch.")
        key = name.strip().lower()
        argv = self._apps.get(key)
        if not argv:
            return AppLaunchResult(
                False,
                f"'{name}' is not on the whitelist. Add it to data/extra_apps.json.",
            )
        try:
            # Explicitly NO shell=True. We pass an argv list — cannot be injected.
            subprocess.Popen(argv, shell=False)
            _log.info("Launched %s -> %s", key, argv)
            return AppLaunchResult(True, f"Launching {name}.")
        except FileNotFoundError:
            return AppLaunchResult(False, f"I couldn't find {name} on this system.")
        except OSError as exc:
            return AppLaunchResult(False, f"Failed to launch {name}: {exc}")

    def is_known(self, name: str) -> bool:
        return name.strip().lower() in self._apps
