"""Category-based permission system with persistent memory.

Categories:
    * ``FILE_READ``       — list / search / open folder
    * ``FILE_WRITE``      — create / rename / write text
    * ``APP_LAUNCH``      — launch whitelisted applications
    * ``SYSTEM_CONTROL``  — shutdown / restart / sleep

Decisions are tri-state: ``"always"``, ``"never"``, or transient (asked again
next time, i.e. **not** persisted).

The actual *prompting* is delegated to a callable injected at construction so
that the same permission engine can be driven by:

    * a CLI ``input()`` loop,
    * the GUI dashboard, or
    * a voice-confirmation flow.
"""
from __future__ import annotations

import json
import threading
from enum import Enum
from typing import Callable, Dict, Optional

from utils.logger import get_logger
from utils.paths import PERMISSIONS_FILE, ensure_dirs

_log = get_logger()


class PermissionCategory(str, Enum):
    """Sensitive action categories the assistant can request access for."""

    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    APP_LAUNCH = "APP_LAUNCH"
    SYSTEM_CONTROL = "SYSTEM_CONTROL"

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_DESCRIPTIONS: Dict[PermissionCategory, str] = {
    PermissionCategory.FILE_READ:
        "read files and folders (listing, searching, opening)",
    PermissionCategory.FILE_WRITE:
        "create, rename, or write into files and folders",
    PermissionCategory.APP_LAUNCH:
        "launch whitelisted desktop applications",
    PermissionCategory.SYSTEM_CONTROL:
        "control the system (shutdown, restart, sleep)",
}


# A prompter takes the category being requested and returns the user's
# decision. The decision must be one of {"yes", "no", "always", "never"}.
PromptFn = Callable[[PermissionCategory], str]


class PermissionDeniedError(Exception):
    """Raised when an action is attempted without consent."""


class PermissionManager:
    """Asks for, caches, and persists user permission decisions."""

    PERSISTENT_VALUES = {"always", "never"}

    def __init__(self, prompter: PromptFn) -> None:
        ensure_dirs()
        self._prompter = prompter
        self._lock = threading.Lock()
        self._persisted: Dict[str, str] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, str]:
        if not PERMISSIONS_FILE.exists():
            return {}
        try:
            with PERMISSIONS_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if v in self.PERSISTENT_VALUES}
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("Permissions file unreadable (%s) — starting fresh", exc)
            return {}

    def _save(self) -> None:
        try:
            tmp = PERMISSIONS_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._persisted, fh, indent=2)
            tmp.replace(PERMISSIONS_FILE)
        except OSError as exc:
            _log.error("Could not save permissions file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def request(self, category: PermissionCategory) -> bool:
        """Return True if ``category`` is currently granted, False otherwise.

        If the user previously said *always* / *never*, that decision is reused
        without re-prompting. Otherwise the prompter is invoked.
        """
        with self._lock:
            stored = self._persisted.get(category.value)

        if stored == "always":
            _log.debug("Permission %s previously granted (always).", category.value)
            return True
        if stored == "never":
            _log.debug("Permission %s previously denied (never).", category.value)
            return False

        decision = (self._prompter(category) or "").strip().lower()
        _log.info("User responded '%s' for %s", decision, category.value)

        granted = decision in {"yes", "y", "always", "ok", "sure"}

        if decision in self.PERSISTENT_VALUES:
            with self._lock:
                self._persisted[category.value] = decision
                self._save()

        return granted

    def ensure(self, category: PermissionCategory) -> None:
        """Like :meth:`request`, but raises :class:`PermissionDeniedError`."""
        if not self.request(category):
            raise PermissionDeniedError(
                f"Permission '{category.value}' was denied by the user."
            )

    def reset(self, category: Optional[PermissionCategory] = None) -> None:
        """Clear stored decisions for one category, or all of them."""
        with self._lock:
            if category is None:
                self._persisted.clear()
                _log.info("All permissions reset.")
            else:
                self._persisted.pop(category.value, None)
                _log.info("Permission %s reset.", category.value)
            self._save()

    def snapshot(self) -> Dict[str, str]:
        """Return a copy of all currently persisted decisions."""
        with self._lock:
            return dict(self._persisted)

    def set_decision(
        self, category: PermissionCategory, decision: Optional[str]
    ) -> None:
        """Programmatically set or clear a category's decision.

        ``decision`` must be ``"always"``, ``"never"`` or ``None`` (clear).
        Used by the Settings dialog to let the user manage permissions
        without waiting for the next prompt.
        """
        with self._lock:
            if decision is None:
                self._persisted.pop(category.value, None)
            elif decision in self.PERSISTENT_VALUES:
                self._persisted[category.value] = decision
            else:
                raise ValueError(
                    f"decision must be 'always', 'never', or None — got {decision!r}"
                )
            self._save()
        _log.info("Permission %s set to %s.", category.value, decision)
