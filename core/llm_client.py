"""LLM brain — turns natural-language requests into JARVIS actions.

Two backends are supported, picked from the ``LLM_PROVIDER`` env var
(or auto-detected from the configured base URL):

* ``"gemini"`` — native Google Gemini SDK (``google-genai``). Best for
  Google AI Studio keys; uses the SDK's structured-JSON support and
  multi-turn chat. See :mod:`core.gemini_client`.
* ``"openai"`` *(default)* — any OpenAI-compatible Chat Completions
  endpoint (OpenAI proper, OpenRouter, Groq, Together, Ollama, …).
  Implemented inline in this module via :mod:`requests`.

Public API:
    Both backends return the same :class:`LLMResponse` dataclass and
    expose ``is_configured`` / ``status()`` / ``reset_history()`` /
    ``interpret(text, user_name=...)``. The :class:`LLMClient` façade
    delegates to whichever backend is currently configured so the rest
    of the codebase only ever touches one type.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import get_logger

_log = get_logger()


# ---------------------------------------------------------------------------
# System prompt — the contract between JARVIS and the LLM.
# Keep this list in sync with executor._dispatch().
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are JARVIS, a polite, witty, and highly capable Windows desktop
assistant inspired by Iron Man's J.A.R.V.I.S. You are conversational but
concise — usually one or two short sentences.

You are the **primary command interpreter** for JARVIS. The user may
speak or type in any language (English, Urdu, Hindi, Arabic, French,
Spanish, etc.). Your job is to translate their request into ONE of the
structured action intents below — or, if the request is just chat,
reply naturally.

Two response modes:

1. ACTION — the user wants you to actually do something on their PC.
2. CHAT   — the user just wants a conversation, a fact, an opinion, etc.

Always respond with a single JSON object (no markdown, no code fences):

    {"mode": "action", "intent": "<one of the intents below>", "args": {...}, "reply": "<short spoken confirmation>"}
    {"mode": "chat",   "reply": "<your spoken reply>"}

`reply` is what JARVIS will speak aloud. ALWAYS write `reply` in
**English** (the TTS engine speaks English). Keep it under 18 words.

Supported action intents and their args:

  open_folder          {"path": "<folder>"}                e.g. "Documents", "D:\\Stuff"
  list_dir             {"path": "<folder>"}
  open_last_path       {}                                  open the most recently created/touched folder
  list_last_path       {}                                  list the most recently created/touched folder
  search_file          {"pattern": "<text>", "roots": ["<folder-or-drive>"]?}
                       Omit "roots" to search every drive in parallel. Pass a
                       drive ("C:", "D drive") or folder to scope the search.
  create_folder        {"name": "<name>", "parent": "<folder>"}
  create_file          {"name": "<filename>", "parent": "<folder>", "contents": "<text>"}
  rename               {"src": "<path>", "new_name": "<new filename>"}
  copy                 {"src": "<path>", "dest_dir": "<folder>"}
  move                 {"src": "<path>", "dest_dir": "<folder>"}
  delete               {"path": "<path>"}
  open_cmd             {"path": "<folder>"}
  launch_app           {"name": "<app>"}                   chrome, notepad, calc, vscode, …
  open_url             {"url": "<https://...>"}
  web_search           {"query": "<text>"}
  shutdown             {}                                  always confirms first
  restart              {}
  sleep                {}
  lock                 {}
  log_off              {}
  cancel_shutdown      {}
  schedule_then        {"seconds": <int>, "then": "<follow-up command in english>"}
  schedule_until_process {"process": "<name.exe>", "then": "<follow-up command>"}
  get_time             {}
  get_date             {}
  tell_joke            {}
  calculate            {"expression": "<simple math>"}     no functions, just + - * / ( )
  system_info          {}
  screenshot           {}
  volume_up            {}
  volume_down          {}
  volume_mute          {}
  show_gui             {}
  hide_gui             {}
  set_name             {"name": "<name>"}
  get_name             {}
  reset_permissions    {}
  list_apps            {}
  help                 {}
  exit                 {}
  recall_history       {"keyword": "<text>"?, "intent_kind": "<kind>"?, "days": <int 1-30>?}
                       Use when the user asks about something they've done in the
                       last 30 days ("did I delete that file yesterday?", "what did
                       I do this week?", "have I created any folders today?"). The
                       assistant remembers up to 30 days of commands; older ones
                       are gone. `intent_kind` (one of: delete, create_file,
                       create_folder, rename, move, copy, search_file, open_folder,
                       launch_app, screenshot) narrows by action; `keyword` matches
                       file/app names; `days` defaults to 30. NEVER use this to
                       actually perform the action — only to look it up.
  clarify              {"for": "<intent_kind>"}
                       Emit this when the user's request is missing essential details
                       (e.g. they say "create a file" with no name/folder, "delete file"
                       with no target, "rename" with no source, "search" with no query).
                       JARVIS will ask the user a tailored follow-up question instead of
                       guessing. `for` must be the kind of action they were trying to do
                       (one of: create_file, create_folder, delete, rename, copy, move,
                       search_file, open_folder, launch_app).

Rules:
  * Resolve named folders ("documents", "downloads", "desktop", "home") to those bare keywords.
  * Never invent paths, file names, or app names the user didn't mention. If even one
    essential argument is missing, prefer `clarify` over guessing.
  * For pronouns like "open it", "go there", "show me that folder", emit `open_last_path`
    (or `list_last_path`). The system already remembers the last folder JARVIS touched.
  * If the request is destructive (delete / rename / overwrite / shutdown), do NOT confirm
    yourself — the assistant will ask the user separately. Just emit the intent.
  * Refuse politely (CHAT mode) for anything that could harm the system,
    edit C:\\Windows or C:\\Program Files, or run arbitrary shell commands.

Examples:

  user: "what time is it"
  -> {"mode":"action","intent":"get_time","args":{},"reply":"Looking at the clock now."}

  user: "abhi kitne baje hain"   # Urdu/Hindi: "what time is it now"
  -> {"mode":"action","intent":"get_time","args":{},"reply":"Checking the clock."}

  user: "open chrome please"
  -> {"mode":"action","intent":"launch_app","args":{"name":"chrome"},"reply":"Launching Chrome."}

  user: "documents mein Demo naam ka folder banao"   # Urdu: create folder Demo in Documents
  -> {"mode":"action","intent":"create_folder","args":{"name":"Demo","parent":"documents"},"reply":"Creating Demo in your Documents."}

  user: "open it"   (after just creating a folder)
  -> {"mode":"action","intent":"open_last_path","args":{},"reply":"Opening it now."}

  user: "tell me a joke"
  -> {"mode":"action","intent":"tell_joke","args":{},"reply":"Here's a good one."}

  user: "who built the pyramids"
  -> {"mode":"chat","reply":"Most historians credit ancient Egyptian workers — paid laborers, not slaves — under Pharaoh Khufu around 2560 BC."}

  user: "what's 12 times 7"
  -> {"mode":"action","intent":"calculate","args":{"expression":"12*7"},"reply":"That's eighty-four."}

  user: "create a file"           # vague — no name, no folder
  -> {"mode":"action","intent":"clarify","args":{"for":"create_file"},"reply":"Sure — what name and where?"}

  user: "delete file"             # vague — no target
  -> {"mode":"action","intent":"clarify","args":{"for":"delete"},"reply":"Which file should I delete?"}

  user: "ek file banao"           # Urdu/Hindi: "make a file" — still vague
  -> {"mode":"action","intent":"clarify","args":{"for":"create_file"},"reply":"Of course — what name, and which folder?"}

  user: "did i delete that resume yesterday"
  -> {"mode":"action","intent":"recall_history","args":{"keyword":"resume","intent_kind":"delete","days":2},"reply":"Let me check your history."}

  user: "what did i do today"
  -> {"mode":"action","intent":"recall_history","args":{"days":1},"reply":"Looking up today's activity."}

  user: "have i opened chrome this week"
  -> {"mode":"action","intent":"recall_history","args":{"keyword":"chrome","intent_kind":"launch_app","days":7},"reply":"Checking my logs."}
"""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    """Parsed LLM reply."""

    mode: str                              # "action" | "chat" | "error"
    reply: str = ""
    intent: Optional[str] = None
    args: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-call system prompt builder (adds runtime context: name, last folder)
# ---------------------------------------------------------------------------
def _build_system_prompt(user_name: Optional[str],
                         last_path: Optional[str]) -> str:
    """Compose the system prompt with the current per-call context."""
    extras: list[str] = []
    if user_name:
        extras.append(
            f"The user's name is {user_name}. Address them naturally."
        )
    if last_path:
        extras.append(
            "Most recent folder JARVIS has touched (use this when the "
            "user says 'open it', 'go there', 'show that folder', etc.): "
            f"{last_path}"
        )
    if not extras:
        return SYSTEM_PROMPT
    return "\n".join(extras) + "\n\n" + SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------
def detect_provider() -> str:
    """Return ``"gemini"`` or ``"openai"`` based on env configuration.

    Order of precedence:
      1. Explicit ``LLM_PROVIDER`` env var.
      2. Presence of a Gemini-specific key (``GEMINI_API_KEY`` etc.).
      3. The configured ``OPENAI_BASE_URL`` pointing at Google's host.
      4. Default to ``"openai"`` (covers OpenAI / Groq / OpenRouter / …).
    """
    explicit = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if explicit in ("gemini", "google", "google-genai"):
        return "gemini"
    if explicit in ("openai", "openai-compat", "compat"):
        return "openai"

    if (os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")):
        return "gemini"

    base = (os.environ.get("OPENAI_BASE_URL") or "").lower()
    if "generativelanguage.googleapis.com" in base or "googleapis" in base:
        # User configured the OpenAI-compatible Gemini URL — prefer the
        # native SDK because it handles JSON mode + multi-turn better.
        return "gemini"

    return "openai"


# ---------------------------------------------------------------------------
# Shared JSON parser (used by both backends)
# ---------------------------------------------------------------------------
def _parse_json_reply(text: str) -> Optional[LLMResponse]:
    """Parse the JSON object the LLM is supposed to return."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    mode = (obj.get("mode") or "").strip().lower()
    reply = (obj.get("reply") or "").strip()
    if mode == "action":
        intent = (obj.get("intent") or "").strip()
        args = obj.get("args") or {}
        if not intent:
            return None
        if not isinstance(args, dict):
            args = {}
        return LLMResponse(mode="action", reply=reply,
                           intent=intent, args=args)
    if mode == "chat":
        return LLMResponse(mode="chat", reply=reply or cleaned)

    return LLMResponse(mode="chat", reply=reply or cleaned)


# ---------------------------------------------------------------------------
# OpenAI-compatible backend
# ---------------------------------------------------------------------------
class OpenAICompatBackend:
    """OpenAI-compatible chat client with conversation memory."""

    def __init__(
        self,
        max_history: int = 8,
        request_timeout: float = 20.0,
    ) -> None:
        self._lock = threading.Lock()
        self._history: List[dict] = []
        self._max_history = max(2, int(max_history))
        self._timeout = float(request_timeout)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key())

    def status(self) -> str:
        if not self.is_configured:
            return "LLM disabled (no API key)"
        return f"OpenAI-compat ready ({self._model()} via {self._base_url()})"

    @staticmethod
    def _api_key() -> str:
        return (os.environ.get("OPENAI_API_KEY") or "").strip()

    @staticmethod
    def _base_url() -> str:
        return (os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")

    @staticmethod
    def _model() -> str:
        return os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

    def reset_history(self) -> None:
        with self._lock:
            self._history.clear()

    def interpret(self, user_message: str,
                  user_name: Optional[str] = None,
                  last_path: Optional[str] = None) -> Optional[LLMResponse]:
        if not self.is_configured:
            return None
        if not user_message or not user_message.strip():
            return None

        sys_prompt = _build_system_prompt(user_name, last_path)

        with self._lock:
            messages = (
                [{"role": "system", "content": sys_prompt}]
                + list(self._history)
                + [{"role": "user", "content": user_message.strip()}]
            )

        raw = self._chat(messages)
        if raw is None:
            return None

        parsed = _parse_json_reply(raw)
        if parsed is None:
            with self._lock:
                self._history.append({"role": "user",
                                      "content": user_message.strip()})
                self._history.append({"role": "assistant", "content": raw})
                self._trim_history_locked()
            return LLMResponse(mode="chat", reply=raw.strip())

        with self._lock:
            self._history.append({"role": "user",
                                  "content": user_message.strip()})
            self._history.append({"role": "assistant", "content": raw})
            self._trim_history_locked()
        return parsed

    def _trim_history_locked(self) -> None:
        keep = self._max_history * 2
        if len(self._history) > keep:
            self._history = self._history[-keep:]

    def _chat(self, messages: list[dict]) -> Optional[str]:
        try:
            import requests  # noqa: PLC0415
        except ImportError:
            _log.warning("requests not installed — LLM disabled.")
            return None

        url = f"{self._base_url()}/chat/completions"
        payload = {
            "model": self._model(),
            "messages": messages,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=self._timeout)
        except requests.RequestException as exc:
            _log.error("LLM request error: %s", exc)
            return None

        if resp.status_code == 400 and "response_format" in resp.text:
            payload.pop("response_format", None)
            try:
                resp = requests.post(url, json=payload, headers=headers,
                                     timeout=self._timeout)
            except requests.RequestException as exc:
                _log.error("LLM retry error: %s", exc)
                return None

        if resp.status_code != 200:
            _log.warning("LLM HTTP %s: %s", resp.status_code,
                         resp.text[:200].replace("\n", " "))
            return None

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            _log.error("LLM bad response: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Façade — picks the right backend at construction time
# ---------------------------------------------------------------------------
class LLMClient:
    """Provider-agnostic façade. Pick OpenAI-compat or native Gemini."""

    def __init__(
        self,
        max_history: int = 8,
        request_timeout: float = 20.0,
    ) -> None:
        provider = detect_provider()
        if provider == "gemini":
            # Local import so users without google-genai installed can
            # still run the OpenAI-compat path without ImportError noise.
            from core.gemini_client import GeminiBackend  # noqa: PLC0415

            self._backend = GeminiBackend(max_history=max_history)
            self.provider = "gemini"
        else:
            self._backend = OpenAICompatBackend(
                max_history=max_history,
                request_timeout=request_timeout,
            )
            self.provider = "openai"

    @property
    def is_configured(self) -> bool:
        return self._backend.is_configured

    def status(self) -> str:
        return self._backend.status()

    def reset_history(self) -> None:
        self._backend.reset_history()

    def interpret(self, user_message: str,
                  user_name: Optional[str] = None,
                  last_path: Optional[str] = None) -> Optional[LLMResponse]:
        return self._backend.interpret(
            user_message, user_name=user_name, last_path=last_path,
        )
