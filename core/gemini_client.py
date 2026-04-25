"""Native Google Gemini brain — uses the official google-genai SDK.

JARVIS speaks two LLM dialects:

* **OpenAI-compatible** (handled by :class:`core.llm_client.OpenAICompatBackend`)
  for OpenAI, OpenRouter, Groq, Together, local Ollama, etc.
* **Native Google Gemini** (this module). The OpenAI-compatible Gemini
  endpoint works fine for plain chat but it occasionally rejects the
  ``response_format=json_object`` we ask for and its conversation
  history handling is different. Using the official SDK gives us:

    * proper conversation memory via :meth:`chats.create`,
    * ``response_mime_type='application/json'`` for guaranteed structured
      replies,
    * the modern ``gemini-2.5-*`` model lineage straight from
      Google AI Studio (paste the API key, pick a model, done).

Public surface mirrors :class:`core.llm_client.LLMClient` so the executor
doesn't care which backend is in use — it just gets an
:class:`LLMResponse`.
"""
from __future__ import annotations

import json
import os
import threading
from typing import List, Optional

from utils.logger import get_logger

_log = get_logger()


# Re-export the prompt builder so the native client uses the same intent
# contract — and the same per-call context extras — the OpenAI-compatible
# client does.
from core.llm_client import (  # noqa: E402
    LLMResponse,
    _build_system_prompt,
    _parse_json_reply,
)


class GeminiBackend:
    """Native Google Gemini chat client with rolling conversation memory."""

    def __init__(self, max_history: int = 8) -> None:
        self._lock = threading.Lock()
        # Stored as plain (role, text) tuples so we can rebuild the chat
        # session lazily without holding live SDK objects across reloads.
        self._history: List[tuple[str, str]] = []
        self._max_history = max(2, int(max_history))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self._api_key())

    def status(self) -> str:
        if not self.is_configured:
            return "Gemini disabled (no API key)"
        return f"Gemini ready ({self._model()})"

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _api_key() -> str:
        # Prefer GEMINI_API_KEY (what Google AI Studio calls it) but
        # accept GOOGLE_API_KEY too for users who already have that env
        # var set, and fall back to the shared OPENAI_API_KEY slot used
        # by the settings dialog.
        return (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()

    @staticmethod
    def _model() -> str:
        return (
            os.environ.get("GEMINI_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "gemini-2.5-flash"
        ).strip()

    def reset_history(self) -> None:
        with self._lock:
            self._history.clear()

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def interpret(self, user_message: str,
                  user_name: Optional[str] = None,
                  last_path: Optional[str] = None) -> Optional[LLMResponse]:
        if not self.is_configured:
            return None
        if not user_message or not user_message.strip():
            return None

        try:
            from google import genai  # noqa: PLC0415
            from google.genai import types  # noqa: PLC0415
        except ImportError:
            _log.warning("google-genai not installed — Gemini disabled. "
                         "Run: pip install google-genai")
            return None

        sys_prompt = _build_system_prompt(user_name, last_path)

        try:
            client = genai.Client(api_key=self._api_key())
        except Exception as exc:  # noqa: BLE001
            _log.error("Gemini client init failed: %s", exc)
            return None

        # Rebuild the conversation each call so the SDK's stateful
        # `chats.create` plays nicely with our cap-N rolling memory.
        with self._lock:
            history = list(self._history)
        contents = []
        for role, text in history:
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=text)],
                )
            )
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_message.strip())],
            )
        )

        config = types.GenerateContentConfig(
            system_instruction=sys_prompt,
            response_mime_type="application/json",
            temperature=0.4,
        )

        try:
            resp = client.models.generate_content(
                model=self._model(),
                contents=contents,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("Gemini request failed: %s", exc)
            # Retry once without JSON mode in case the model rejects it.
            try:
                config_plain = types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    temperature=0.4,
                )
                resp = client.models.generate_content(
                    model=self._model(),
                    contents=contents,
                    config=config_plain,
                )
            except Exception as exc2:  # noqa: BLE001
                _log.error("Gemini retry failed: %s", exc2)
                return None

        raw = (getattr(resp, "text", "") or "").strip()
        if not raw:
            # Fall back to combing through candidates if .text is empty.
            try:
                cands = getattr(resp, "candidates", None) or []
                for c in cands:
                    parts = getattr(getattr(c, "content", None), "parts",
                                    None) or []
                    for p in parts:
                        t = getattr(p, "text", None)
                        if t:
                            raw = t.strip()
                            break
                    if raw:
                        break
            except Exception:  # noqa: BLE001
                pass
        if not raw:
            _log.warning("Gemini returned an empty reply.")
            return None

        parsed = _parse_json_reply(raw)
        with self._lock:
            self._history.append(("user", user_message.strip()))
            # Gemini's chat role for assistant turns is "model".
            self._history.append(("model", raw))
            keep = self._max_history * 2
            if len(self._history) > keep:
                self._history = self._history[-keep:]

        if parsed is None:
            return LLMResponse(mode="chat", reply=raw)
        return parsed
