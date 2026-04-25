"""Logging facility for JARVIS.

Behaviour:
    * One log file per UTC day (``jarvis-YYYY-MM-DD.log``).
    * On every startup we prune any log file older than ``RETENTION_DAYS``
      (default: 7 days), satisfying the spec's privacy requirement.
    * Console handler is colourised when ``colorama`` is available.

Use :func:`get_logger` from anywhere in the codebase — it is idempotent and
returns the same configured logger.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .paths import LOG_DIR, ensure_dirs

RETENTION_DAYS: int = 7
_LOGGER_NAME: str = "jarvis"
_initialised: bool = False


def _prune_old_logs() -> None:
    """Delete log files older than :data:`RETENTION_DAYS`.

    We do this manually (rather than rely solely on TimedRotatingFileHandler's
    ``backupCount``) so logs from previous runs are also cleaned up if the
    assistant has been idle for a while.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=RETENTION_DAYS)
    for entry in LOG_DIR.glob("jarvis-*.log*"):
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _build_handlers() -> list[logging.Handler]:
    """Construct the file + console handlers."""
    ensure_dirs()
    log_path: Path = LOG_DIR / "jarvis.log"

    file_handler = TimedRotatingFileHandler(
        filename=log_path,
        when="midnight",
        interval=1,
        backupCount=RETENTION_DAYS,
        encoding="utf-8",
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)

    try:
        from colorama import Fore, Style, init as colorama_init

        colorama_init(autoreset=True)

        class _ColourFormatter(logging.Formatter):
            COLOURS = {
                "DEBUG": Fore.CYAN,
                "INFO": Fore.GREEN,
                "WARNING": Fore.YELLOW,
                "ERROR": Fore.RED,
                "CRITICAL": Fore.MAGENTA + Style.BRIGHT,
            }

            def format(self, record: logging.LogRecord) -> str:
                colour = self.COLOURS.get(record.levelname, "")
                msg = super().format(record)
                return f"{colour}{msg}{Style.RESET_ALL}" if colour else msg

        console.setFormatter(
            _ColourFormatter("[%(levelname)s] %(message)s")
        )
    except ImportError:
        console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    return [file_handler, console]


def get_logger() -> logging.Logger:
    """Return the singleton JARVIS logger, initialising it on first call."""
    global _initialised
    logger = logging.getLogger(_LOGGER_NAME)

    if not _initialised:
        _prune_old_logs()
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        for h in _build_handlers():
            logger.addHandler(h)
        logger.propagate = False
        _initialised = True

    return logger
