"""Persistent rolling command history.

The on-disk representation is a single JSON file. Two independent caps
keep the file from growing without bound:

* ``MAX_ENTRIES`` (500) — hard upper bound that protects us from a
  flood of commands inside a single day.
* ``RETENTION_DAYS`` (30) — *time-based* TTL. Anything older than this
  many days is purged on load, on every new entry, and whenever the UI
  asks for a day-wise view. A user can therefore scroll back through
  exactly one month of activity, no more — older entries are forgotten
  forever.

The :meth:`History.by_day` and :meth:`History.find` helpers power the
new "history viewer" dialog and the spoken ``recall_history`` intent
that lets the user ask things like *"did I delete that file
yesterday?"* or *"what did I do this week?"*.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from utils.logger import get_logger
from utils.paths import HISTORY_FILE, ensure_dirs

_log = get_logger()

MAX_ENTRIES: int = 500

# Hard TTL for stored chats: anything older than this many days is
# dropped (on load, on every add, and whenever a day-wise view is
# requested). Kept as a module constant so tests can monkey-patch it.
RETENTION_DAYS: int = 30


@dataclass
class HistoryEntry:
    """One row in the history viewer."""

    timestamp: str            # ISO-8601 UTC
    user_text: str            # raw transcript / typed input
    intent_kind: str
    response: str
    success: bool


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
def _parse_utc(ts_iso: str) -> Optional[datetime]:
    """Parse a stored ISO timestamp back to a UTC-aware datetime."""
    try:
        dt = datetime.fromisoformat(ts_iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def entry_local_dt(entry: HistoryEntry) -> Optional[datetime]:
    """Return the entry's timestamp converted to the user's local timezone."""
    dt = _parse_utc(entry.timestamp)
    return dt.astimezone() if dt else None


def format_local(ts_iso: str) -> str:
    """Format a stored ISO timestamp as a friendly local string."""
    dt = _parse_utc(ts_iso)
    if dt is None:
        return ts_iso or "?"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class History:
    """Thread-safe rolling list of :class:`HistoryEntry`."""

    def __init__(self) -> None:
        ensure_dirs()
        self._lock = threading.Lock()
        self._entries: List[HistoryEntry] = self._load()
        self._listeners: List[Callable[[HistoryEntry], None]] = []
        # Drop anything past the retention window the moment we boot.
        # (`_load` already trims, but does so without holding the lock.)
        with self._lock:
            self._purge_old()
            if self._entries:
                self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> List[HistoryEntry]:
        if not HISTORY_FILE.exists():
            return []
        try:
            with HISTORY_FILE.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            entries = [HistoryEntry(**row) for row in raw[-MAX_ENTRIES:]]
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            _log.warning("History file unreadable (%s) — starting fresh", exc)
            return []
        # Pre-trim by retention so we never even surface a stale entry
        # to the rest of the app.
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        kept = [e for e in entries
                if (dt := _parse_utc(e.timestamp)) is not None and dt >= cutoff]
        return kept

    def _save(self) -> None:
        try:
            tmp = HISTORY_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump([asdict(e) for e in self._entries], fh, indent=2)
            tmp.replace(HISTORY_FILE)
        except OSError as exc:
            _log.error("Could not save history file: %s", exc)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------
    def _purge_old(self) -> int:
        """Drop entries older than ``RETENTION_DAYS``. Returns count removed.

        Caller must hold ``self._lock``.
        """
        if not self._entries:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        before = len(self._entries)
        self._entries = [
            e for e in self._entries
            if (dt := _parse_utc(e.timestamp)) is not None and dt >= cutoff
        ]
        removed = before - len(self._entries)
        if removed:
            _log.debug("History purge: removed %d entries older than %d days",
                       removed, RETENTION_DAYS)
        return removed

    def purge_old(self) -> int:
        """Public, locked variant of :meth:`_purge_old`."""
        with self._lock:
            removed = self._purge_old()
            if removed:
                self._save()
            return removed

    # ------------------------------------------------------------------
    # Public API — write
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
            # Time-based purge first, then the safety cap.
            self._purge_old()
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

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save()

    # ------------------------------------------------------------------
    # Public API — read
    # ------------------------------------------------------------------
    def latest(self, n: int = 50) -> List[HistoryEntry]:
        with self._lock:
            return list(self._entries[-n:])

    def all(self) -> List[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def by_day(
        self, days: int = RETENTION_DAYS
    ) -> Dict[date, List[HistoryEntry]]:
        """Return entries from the last ``days`` days, grouped by local date.

        Result is an ordered dict: most-recent day first; within each
        day entries are sorted oldest → newest so the UI can render them
        in chronological order under each header.
        """
        days = max(1, min(RETENTION_DAYS, int(days)))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        groups: Dict[date, List[HistoryEntry]] = {}
        with self._lock:
            for entry in self._entries:
                dt = _parse_utc(entry.timestamp)
                if dt is None or dt < cutoff:
                    continue
                local_day = dt.astimezone().date()
                groups.setdefault(local_day, []).append(entry)
        for day in groups:
            groups[day].sort(key=lambda e: e.timestamp)
        # Sort dates newest first.
        return dict(sorted(groups.items(), key=lambda kv: kv[0], reverse=True))

    def find(
        self,
        keyword: Optional[str] = None,
        intent_kind: Optional[str] = None,
        days: int = RETENTION_DAYS,
        success_only: bool = False,
    ) -> List[HistoryEntry]:
        """Search the last ``days`` of history.

        Filters are AND-combined and all optional. Matching is
        case-insensitive and runs against the user's text, the
        assistant's reply, and the intent kind so a query like
        ``keyword="resume"`` will surface "delete resume.pdf" *and*
        "search for resume".
        """
        days = max(1, min(RETENTION_DAYS, int(days)))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        needle = (keyword or "").lower().strip()
        wanted_kind = (intent_kind or "").strip().lower()

        out: List[HistoryEntry] = []
        with self._lock:
            for entry in self._entries:
                dt = _parse_utc(entry.timestamp)
                if dt is None or dt < cutoff:
                    continue
                if success_only and not entry.success:
                    continue
                if wanted_kind and entry.intent_kind.lower() != wanted_kind:
                    continue
                if needle:
                    hay = " ".join((
                        entry.user_text, entry.response, entry.intent_kind
                    )).lower()
                    if needle not in hay:
                        continue
                out.append(entry)
        out.sort(key=lambda e: e.timestamp, reverse=True)
        return out

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------
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
