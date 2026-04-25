"""Centralised path resolution for runtime data.

Keeping all paths in one module avoids hard-coding and makes the assistant
relocatable (matters when packaged with PyInstaller).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _app_root() -> Path:
    """Return the directory the assistant should treat as its install root.

    When frozen by PyInstaller, ``sys.executable`` points at the .exe so we
    sit ``data/`` next to it. Otherwise we use the project root (this file's
    grandparent).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Resolve the runtime data directory.

    Honours the ``JARVIS_DATA_DIR`` environment variable so the user can
    relocate their persisted state (memory / permissions / history /
    logs) to e.g. a synced folder. Tests use the same hook to run
    against a sandbox without touching real user data.
    """
    override = os.environ.get("JARVIS_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return APP_ROOT / "data"


APP_ROOT: Path = _app_root()
DATA_DIR: Path = _data_dir()
LOG_DIR: Path = DATA_DIR / "logs"
MEMORY_FILE: Path = DATA_DIR / "memory.json"
PERMISSIONS_FILE: Path = DATA_DIR / "permissions.json"
HISTORY_FILE: Path = DATA_DIR / "history.json"


def ensure_dirs() -> None:
    """Create the runtime data directories if they don't already exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def user_home() -> Path:
    """Return the current user's home directory in a cross-platform way."""
    return Path(os.path.expanduser("~"))
