"""Power-state operations: shutdown, restart, lock, sleep, log off.

Every action requires explicit confirmation **every time** (no "always"
shortcut), per the spec.
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from utils.logger import get_logger

_log = get_logger()

ConfirmFn = Callable[[str], bool]


@dataclass
class SystemActionResult:
    ok: bool
    message: str


class SystemController:
    """Cross-platform-ish wrapper around shutdown/restart/sleep/lock."""

    def __init__(self, confirmer: Optional[ConfirmFn] = None) -> None:
        self._confirm = confirmer or (lambda _q: False)

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------
    def shutdown(self, delay_seconds: int = 5) -> SystemActionResult:
        if not self._confirm(
            f"Shut down the PC in {delay_seconds} seconds? Say yes to confirm."
        ):
            return SystemActionResult(False, "Shutdown cancelled.")
        return self._run_shutdown(["/s", "/t", str(int(delay_seconds))],
                                  "Shutting down.")

    def restart(self, delay_seconds: int = 5) -> SystemActionResult:
        if not self._confirm(
            f"Restart the PC in {delay_seconds} seconds? Say yes to confirm."
        ):
            return SystemActionResult(False, "Restart cancelled.")
        return self._run_shutdown(["/r", "/t", str(int(delay_seconds))],
                                  "Restarting.")

    def log_off(self) -> SystemActionResult:
        if not self._confirm("Log off the current user? Say yes to confirm."):
            return SystemActionResult(False, "Log off cancelled.")
        return self._run_shutdown(["/l"], "Logging off.")

    def sleep(self) -> SystemActionResult:
        if not self._confirm("Put the PC to sleep? Say yes to confirm."):
            return SystemActionResult(False, "Sleep cancelled.")
        if not sys.platform.startswith("win"):
            return SystemActionResult(False, "Sleep is only implemented on Windows.")
        try:
            # rundll32 powrprof — the documented way to suspend Windows.
            subprocess.Popen(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                shell=False,
            )
            _log.info("Issued sleep command.")
            return SystemActionResult(True, "Going to sleep.")
        except OSError as exc:
            return SystemActionResult(False, f"Could not sleep: {exc}")

    def lock(self) -> SystemActionResult:
        if not self._confirm("Lock the workstation?"):
            return SystemActionResult(False, "Lock cancelled.")
        if not sys.platform.startswith("win"):
            return SystemActionResult(False, "Lock is only implemented on Windows.")
        try:
            ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
            return SystemActionResult(True, "Workstation locked.")
        except OSError as exc:
            return SystemActionResult(False, f"Could not lock: {exc}")

    def cancel_pending_shutdown(self) -> SystemActionResult:
        if not sys.platform.startswith("win"):
            return SystemActionResult(False, "Only supported on Windows.")
        try:
            subprocess.run(["shutdown", "/a"], check=False, shell=False)
            return SystemActionResult(True, "Pending shutdown cancelled.")
        except OSError as exc:
            return SystemActionResult(False, f"Could not cancel: {exc}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_shutdown(self, args: list[str], message: str) -> SystemActionResult:
        if not sys.platform.startswith("win"):
            return SystemActionResult(False, "Only supported on Windows.")
        if not shutil.which("shutdown"):
            return SystemActionResult(False, "shutdown.exe not found on this system.")
        try:
            subprocess.run(["shutdown", *args], check=True, shell=False)
            _log.info("System action issued: shutdown %s", " ".join(args))
            return SystemActionResult(True, message)
        except subprocess.CalledProcessError as exc:
            return SystemActionResult(False, f"Command failed: {exc}")
        except OSError as exc:
            return SystemActionResult(False, f"Could not run shutdown: {exc}")
