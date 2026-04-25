"""Persistent user preferences (name + arbitrary key/value prefs).

Stored as a single JSON file at :data:`utils.paths.MEMORY_FILE`. The format is
intentionally human-editable so a curious user can inspect or wipe their data.

Also keeps a small **recent paths** ring (last folders the user created /
opened / listed) so JARVIS can resolve pronouns like *"open it"* or
*"go there"* against the most recent folder it touched.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, List, Optional

from utils.logger import get_logger
from utils.paths import MEMORY_FILE, ensure_dirs

_log = get_logger()

# How many recent folder paths to remember.
_RECENT_PATHS_CAP = 10


class Memory:
    """Thread-safe wrapper around a JSON dict on disk."""

    def __init__(self) -> None:
        ensure_dirs()
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()
        self._listeners: List[Callable[[str, Any], None]] = []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if not MEMORY_FILE.exists():
            return {}
        try:
            with MEMORY_FILE.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("Memory file unreadable (%s) — starting fresh", exc)
            return {}

    def _save(self) -> None:
        try:
            tmp = MEMORY_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            tmp.replace(MEMORY_FILE)
        except OSError as exc:
            _log.error("Could not save memory file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(key, value)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Memory listener raised: %s", exc)

    def delete(self, key: str) -> None:
        fired = False
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._save()
                fired = True
            listeners = list(self._listeners) if fired else []
        for cb in listeners:
            try:
                cb(key, None)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Memory listener raised: %s", exc)

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    # ----- Observer pattern (used by the GUI to live-update widgets) ---
    def subscribe(self, cb: Callable[[str, Any], None]) -> None:
        """Register ``cb(key, value)`` fired whenever a value changes.

        Listeners run **after** the lock is released, so they may safely
        call back into ``Memory`` without deadlocking.
        """
        with self._lock:
            self._listeners.append(cb)

    def unsubscribe(self, cb: Callable[[str, Any], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

    # ----- Convenience: user identity -----
    @property
    def user_name(self) -> Optional[str]:
        return self.get("user_name")

    @user_name.setter
    def user_name(self, value: str) -> None:
        cleaned = (value or "").strip()
        if cleaned:
            self.set("user_name", cleaned)

    def has_user_name(self) -> bool:
        return bool(self.user_name)

    # ----- Recent folders (used by "open it" / "go there" intents) -----
    def remember_path(self, path: str) -> None:
        """Push ``path`` to the front of the recent-paths ring.

        Duplicates are de-duped (already-recent paths bubble to the
        front). Cap at :data:`_RECENT_PATHS_CAP`.
        """
        if not path:
            return
        path = str(path)
        with self._lock:
            recent: List[str] = list(self._data.get("recent_paths") or [])
            recent = [p for p in recent if p != path]
            recent.insert(0, path)
            if len(recent) > _RECENT_PATHS_CAP:
                recent = recent[:_RECENT_PATHS_CAP]
            self._data["recent_paths"] = recent
            self._save()
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb("recent_paths", recent)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Memory listener raised: %s", exc)

    @property
    def recent_paths(self) -> List[str]:
        with self._lock:
            return list(self._data.get("recent_paths") or [])

    @property
    def last_path(self) -> Optional[str]:
        with self._lock:
            recent = self._data.get("recent_paths") or []
            return recent[0] if recent else None

    def clear_recent_paths(self) -> None:
        self.set("recent_paths", [])
