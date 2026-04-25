"""Persistent rolling command history.

Stored as a single JSON file. We cap the in-memory list at ``MAX_ENTRIES`` so
the file never grows without bound.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from utils.logger import get_logger
from utils.paths import HISTORY_FILE, ensure_dirs

_log = get_logger()

MAX_ENTRIES: int = 500


@dataclass
class HistoryEntry:
    """One row in the history viewer."""

    timestamp: str            # ISO-8601 UTC
    user_text: str            # raw transcript / typed input
    intent_kind: str
    response: str
    success: bool


class History:
    """Thread-safe rolling list of :class:`HistoryEntry`."""

    def __init__(self) -> None:
        ensure_dirs()
        self._lock = threading.Lock()
        self._entries: List[HistoryEntry] = self._load()
        self._listeners: List[Callable[[HistoryEntry], None]] = []

    def _load(self) -> List[HistoryEntry]:
        if not HISTORY_FILE.exists():
            return []
        try:
            with HISTORY_FILE.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return [HistoryEntry(**row) for row in raw[-MAX_ENTRIES:]]
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            _log.warning("History file unreadable (%s) — starting fresh", exc)
            return []

    def _save(self) -> None:
        try:
            tmp = HISTORY_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump([asdict(e) for e in self._entries], fh, indent=2)
            tmp.replace(HISTORY_FILE)
        except OSError as exc:
            _log.error("Could not save history file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add(
        self,
        user_text: str,
        intent_kind: str,
        response: str,
        success: bool,
    ) -> HistoryEntry:
        entry = HistoryEntry(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            user_text=user_text or "",
            intent_kind=intent_kind,
            response=response or "",
            success=bool(success),
        )
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > MAX_ENTRIES:
                self._entries = self._entries[-MAX_ENTRIES:]
            self._save()
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(entry)
            except Exception as exc:  # noqa: BLE001
                _log.debug("History listener raised: %s", exc)
        return entry

    def latest(self, n: int = 50) -> List[HistoryEntry]:
        with self._lock:
            return list(self._entries[-n:])

    def all(self) -> List[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save()

    def subscribe(self, cb: Callable[[HistoryEntry], None]) -> None:
        """Register a callback fired every time a new entry is added."""
        with self._lock:
            self._listeners.append(cb)

    def unsubscribe(self, cb: Callable[[HistoryEntry], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass


# Convenience for the GUI / CLI: pretty timestamp.
def format_local(ts_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts_iso or "?"
