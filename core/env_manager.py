"""Read / write the project ``.env`` file.

Why a tiny custom helper instead of ``python-dotenv`` for writes?
    ``python-dotenv`` is great for *reading* env files but its writer changes
    formatting, can lose comments, and rewrites every value. We want to:

    * preserve the user's hand-written comments and ordering,
    * only mutate the keys we actually changed,
    * keep secrets out of the project root if a custom location is set via
      the ``JARVIS_ENV_FILE`` environment variable.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger
from utils.paths import APP_ROOT

_log = get_logger()
_lock = threading.Lock()


def env_file_path() -> Path:
    """Return the canonical path to the .env file."""
    override = os.environ.get("JARVIS_ENV_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return APP_ROOT / ".env"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_env() -> Dict[str, str]:
    """Load the .env file into ``os.environ`` and return its contents."""
    path = env_file_path()
    if not path.exists():
        return {}

    try:
        from dotenv import dotenv_values, load_dotenv  # noqa: PLC0415

        load_dotenv(path, override=False)
        return {k: v or "" for k, v in dotenv_values(path).items()}
    except ImportError:
        _log.debug("python-dotenv not installed; falling back to manual parse.")

    # Manual fallback parser (KEY=value lines, ignores '# ...' and blanks).
    values: Dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                values[key] = val
                os.environ.setdefault(key, val)
    except OSError as exc:
        _log.warning("Could not read %s: %s", path, exc)
    return values


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def set_value(key: str, value: str) -> None:
    """Persist ``key=value`` into the .env file (creates it if missing).

    Existing lines are preserved; only the matching ``KEY=`` line is rewritten.
    Also updates ``os.environ`` so the new value is picked up immediately.
    """
    if not key or not key.replace("_", "").isalnum():
        raise ValueError(f"Invalid env key: {key!r}")

    path = env_file_path()
    new_line = f"{key}={value}"

    with _lock:
        lines: list[str] = []
        replaced = False
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                _log.warning("Could not read existing .env: %s", exc)

        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                lines[i] = new_line
                replaced = True
                break

        if not replaced:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(new_line)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            _log.error("Could not write .env: %s", exc)
            raise

    os.environ[key] = value
    _log.info("Updated .env key %s (%s).", key,
              "set" if value else "cleared")


def get_value(key: str, default: Optional[str] = None) -> Optional[str]:
    """Convenience accessor that prefers ``os.environ`` (already loaded)."""
    return os.environ.get(key, default)


def write_example() -> None:
    """Create ``.env.example`` if it doesn't already exist."""
    sample = APP_ROOT / ".env.example"
    if sample.exists():
        return
    sample.write_text(
        "# JARVIS optional environment configuration\n"
        "# Copy this file to .env and fill in the values you want.\n"
        "#\n"
        "# Two LLM backends are supported, picked from LLM_PROVIDER:\n"
        "#   * gemini  — native google-genai SDK (best for Google AI Studio keys)\n"
        "#   * openai  — any OpenAI-compatible Chat Completions API\n"
        "#\n"
        "# Examples:\n"
        "#\n"
        "#   ---- Google Gemini (key from https://aistudio.google.com) ----\n"
        "#   LLM_PROVIDER=gemini\n"
        "#   GEMINI_API_KEY=AIza...\n"
        "#   GEMINI_MODEL=gemini-2.5-flash\n"
        "#\n"
        "#   ---- OpenAI ----\n"
        "#   LLM_PROVIDER=openai\n"
        "#   OPENAI_API_KEY=sk-...\n"
        "#   OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "#   OPENAI_MODEL=gpt-4o-mini\n"
        "#\n"
        "#   ---- Groq ----\n"
        "#   LLM_PROVIDER=openai\n"
        "#   OPENAI_API_KEY=gsk_...\n"
        "#   OPENAI_BASE_URL=https://api.groq.com/openai/v1\n"
        "#   OPENAI_MODEL=llama-3.1-70b-versatile\n"
        "\n"
        "LLM_PROVIDER=openai\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "OPENAI_MODEL=gpt-4o-mini\n"
        "\n"
        "# Gemini-specific (used when LLM_PROVIDER=gemini)\n"
        "GEMINI_API_KEY=\n"
        "GEMINI_MODEL=gemini-2.5-flash\n",
        encoding="utf-8",
    )
