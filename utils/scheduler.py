"""Lightweight async-style scheduler for deferred / conditional tasks.

JARVIS uses this to support natural-language commands such as:

    * "wait 10 minutes then restart the PC"
    * "wait until chrome.exe ends, then shut down"

Implementation notes:
    * Each task runs on its **own daemon thread** — no event loop is required.
      This keeps the public API trivial to call from synchronous voice
      handlers.
    * Tasks are addressable by an integer id so the GUI / CLI can list and
      cancel them.
    * ``ProcessWaitTask`` polls via :mod:`psutil`; if psutil isn't installed
      we fall back to ``tasklist`` parsing so the assistant stays functional.
"""
from __future__ import annotations

import itertools
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class ScheduledTask:
    """In-memory record of a pending or running scheduled task."""

    id: int
    description: str
    created_at: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    status: str = "pending"  # pending | running | done | cancelled | failed
    error: Optional[str] = None


class Scheduler:
    """Manages background tasks (delays + process-wait conditions)."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._tasks: Dict[int, ScheduledTask] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def schedule_after(
        self,
        delay_seconds: float,
        action: Callable[[], None],
        description: str,
    ) -> ScheduledTask:
        """Run ``action`` after waiting ``delay_seconds``.

        Returns the :class:`ScheduledTask` so callers can cancel it later.
        """
        task = self._new_task(description)

        def _runner() -> None:
            task.status = "running"
            # Sleep in 0.5-second chunks so we can respond to cancel quickly.
            elapsed = 0.0
            while elapsed < delay_seconds:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    return
                time.sleep(min(0.5, delay_seconds - elapsed))
                elapsed += 0.5
            self._safe_run(task, action)

        threading.Thread(target=_runner, daemon=True, name=f"sched-{task.id}").start()
        return task

    def schedule_when_process_ends(
        self,
        process_name: str,
        action: Callable[[], None],
        description: str,
        poll_interval: float = 2.0,
    ) -> ScheduledTask:
        """Wait until *all* processes named ``process_name`` have exited."""
        task = self._new_task(description)

        def _runner() -> None:
            task.status = "running"
            while not task.cancel_event.is_set():
                if not _process_running(process_name):
                    self._safe_run(task, action)
                    return
                time.sleep(poll_interval)
            task.status = "cancelled"

        threading.Thread(target=_runner, daemon=True, name=f"sched-{task.id}").start()
        return task

    def cancel(self, task_id: int) -> bool:
        """Request cancellation of a scheduled task. Returns True if found."""
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            return False
        task.cancel_event.set()
        return True

    def list_tasks(self) -> List[ScheduledTask]:
        """Snapshot of all known tasks (in id order)."""
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _new_task(self, description: str) -> ScheduledTask:
        with self._lock:
            tid = next(self._counter)
            task = ScheduledTask(
                id=tid,
                description=description,
                created_at=time.time(),
            )
            self._tasks[tid] = task
        return task

    @staticmethod
    def _safe_run(task: ScheduledTask, action: Callable[[], None]) -> None:
        try:
            action()
            task.status = "done"
        except Exception as exc:  # noqa: BLE001 - we want to surface any failure
            task.status = "failed"
            task.error = str(exc)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _process_running(name: str) -> bool:
    """Return True if any running process is named ``name`` (case-insensitive)."""
    name = name.lower()
    if not name.endswith(".exe"):
        name_with_ext = name + ".exe"
    else:
        name_with_ext = name

    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["name"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if pname in (name, name_with_ext):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except ImportError:
        pass

    # Fallback: shell out to `tasklist` (Windows only).
    if not shutil.which("tasklist"):
        return False
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name_with_ext}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout.lower()
        return name_with_ext in output
    except (subprocess.SubprocessError, OSError):
        return False
