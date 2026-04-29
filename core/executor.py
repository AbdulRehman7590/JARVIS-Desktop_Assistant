"""Executor: maps :class:`Intent` → real-world action.

The executor is the **only** module that talks to both the permission system
and the system-level modules. Voice / GUI front-ends only interact with
:meth:`Executor.handle`.

When the rule-based parser returns ``unknown`` and an LLM is configured,
the executor asks the LLM to either suggest a structured intent or reply
conversationally. This gives JARVIS a natural-language "brain" without
sacrificing speed for the common hardcoded commands.

Contract:
    * :meth:`handle` always returns an :class:`ExecutionResult` — never raises
      to the caller. Errors are turned into spoken-friendly messages.
    * Every call is logged to :class:`History` so the GUI can show a transcript.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.command_parser import CommandParser, Intent
from core.history import History, HistoryEntry, RETENTION_DAYS, entry_local_dt
from core.llm_client import LLMClient
from core.memory import Memory
from core.permissions import (
    PermissionCategory,
    PermissionDeniedError,
    PermissionManager,
)
from system import utilities as util_actions
from system.app_launcher import AppLauncher
from system.file_manager import FileManager, FileSafetyError
from system.system_control import SystemController
from utils.logger import get_logger
from utils.scheduler import Scheduler

_log = get_logger()

# Intents the rule parser is *fully* trusted to handle without consulting
# the LLM. Anything outside this set goes through the LLM first when it's
# configured (so JARVIS understands free-form phrasing in any language)
# and falls back to the rule output if the LLM is off or unreachable.
_TRUSTED_FAST_INTENTS = frozenset({
    "exit", "help", "greet", "thanks", "how_are_you", "who_are_you",
    "get_time", "get_date", "tell_joke", "system_info", "screenshot",
    "volume_up", "volume_down", "volume_mute",
    "show_gui", "hide_gui",
    "open_last_path", "list_last_path",
    "reset_permissions", "list_apps",
    "cancel_shutdown",
    "clarify",
    "recall_history",
})


# Voice-friendly counter-questions for vague commands. Both the rule
# parser and the LLM emit ``Intent("clarify", {"for": "<intent>"})``
# when the user's request is missing essential details (e.g. "create
# file" with no name or destination). These prompts MUST stay short —
# they are spoken aloud — and end with a clear question so the user
# knows what to say next.
_CLARIFY_PROMPTS: Dict[str, str] = {
    "create_file":
        "Sure — what should I name the file, and where should I create it?",
    "create_folder":
        "Of course — what name should the new folder have, and where do you want it?",
    "delete":
        "Which file or folder would you like me to delete? "
        "Please tell me a name or full path.",
    "rename":
        "Which file should I rename, and what's the new name?",
    "copy":
        "Which file should I copy, and where should I copy it to?",
    "move":
        "Which file should I move, and where should it go?",
    "search_file":
        "What file name or keyword should I search for?",
    "open_folder":
        "Which folder should I open?",
    "launch_app":
        "Which application should I launch?",
}


@dataclass
class ExecutionResult:
    """The user-facing outcome of a handled command."""

    response: str
    success: bool
    intent_kind: str
    should_exit: bool = False
    source: str = "rules"   # "rules" | "llm" | "chat"


class Executor:
    """Dispatch parsed intents to the right subsystem with permission gating."""

    def __init__(
        self,
        memory: Memory,
        permissions: PermissionManager,
        file_manager: FileManager,
        app_launcher: AppLauncher,
        system_controller: SystemController,
        scheduler: Scheduler,
        history: History,
        parser: Optional[CommandParser] = None,
        llm: Optional[LLMClient] = None,
    ) -> None:
        self.memory = memory
        self.permissions = permissions
        self.files = file_manager
        self.apps = app_launcher
        self.system = system_controller
        self.scheduler = scheduler
        self.history = history
        self.parser = parser or CommandParser()
        self.llm = llm or LLMClient()
        # Optional UI hooks injected by the dashboard so voice can hide /
        # show the window. Default no-ops keep CLI / tests happy.
        self.on_show_gui = lambda: False
        self.on_hide_gui = lambda: False

    # ------------------------------------------------------------------
    # Top-level entry point
    # ------------------------------------------------------------------
    def handle(self, user_text: str) -> ExecutionResult:
        """Parse ``user_text`` and execute the corresponding action.

        Routing strategy:

        1. Run the cheap regex parser. If it returns one of the
           ``_TRUSTED_FAST_INTENTS`` (greetings, time/date, screenshot,
           etc.) we dispatch immediately — zero LLM cost, zero latency.
        2. Otherwise, when an LLM is configured, ask it to interpret the
           command. It either returns a structured intent (we dispatch)
           or a chat reply (we speak it). This lets JARVIS understand
           natural-language requests in *any* language Gemini supports
           without us having to maintain hand-tuned regex for each.
        3. If the LLM is unavailable or fails, we fall back to whatever
           the regex parser produced — including ``unknown``.
        """
        intent = self.parser.parse(user_text)
        _log.debug("Parsed intent: %s", intent)
        source = "rules"
        llm_first = (
            self.llm
            and self.llm.is_configured
            and intent.kind not in _TRUSTED_FAST_INTENTS
        )

        if llm_first:
            try:
                llm_resp = self.llm.interpret(
                    user_text,
                    user_name=self.memory.user_name,
                    last_path=self.memory.last_path,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("LLM interpret failed: %s", exc)
                llm_resp = None

            if llm_resp is not None:
                if llm_resp.mode == "action" and llm_resp.intent:
                    _log.info("LLM -> action %s %r",
                              llm_resp.intent, llm_resp.args)
                    intent = Intent(
                        llm_resp.intent, dict(llm_resp.args),
                        raw=user_text,
                    )
                    source = "llm"
                else:
                    reply = llm_resp.reply or "Hm, I'm not sure."
                    result = ExecutionResult(reply, True, "chat",
                                             source="chat")
                    self.history.add(user_text, result.intent_kind,
                                     result.response, result.success)
                    return result
            elif intent.kind == "unknown":
                # LLM was configured but unreachable; nothing else to try.
                pass

        try:
            result = self._dispatch(intent)
            result.source = source
        except PermissionDeniedError as exc:
            result = ExecutionResult(
                response=f"I can't do that without permission. {exc}",
                success=False,
                intent_kind=intent.kind,
                source=source,
            )
        except FileSafetyError as exc:
            result = ExecutionResult(
                response=f"That action is blocked for safety: {exc}",
                success=False,
                intent_kind=intent.kind,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - we never want to crash the loop
            _log.exception("Executor crashed handling %r", intent)
            result = ExecutionResult(
                response=f"Something went wrong: {exc}",
                success=False,
                intent_kind=intent.kind,
                source=source,
            )

        self.history.add(
            user_text=user_text,
            intent_kind=result.intent_kind,
            response=result.response,
            success=result.success,
        )
        return result

    # ------------------------------------------------------------------
    # Intent dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, intent: Intent) -> ExecutionResult:
        kind = intent.kind
        args: Dict[str, Any] = intent.args or {}

        # Bookkeeping intents -------------------------------------------
        if kind == "exit":
            name = self.memory.user_name or "sir"
            return ExecutionResult(f"Goodbye, {name}.", True, kind, should_exit=True)

        # Clarification — the user gave a vague command like "create file"
        # or "delete file" with no target. We bounce a short follow-up
        # question back rather than silently failing or guessing.
        if kind == "clarify":
            target = (args.get("for") or "command").strip()
            prompt = _CLARIFY_PROMPTS.get(
                target,
                "I need a little more detail. Could you tell me which "
                "file or folder, and where?",
            )
            return ExecutionResult(prompt, True, kind)

        # Recall — the user is asking about something they've done in
        # the last 30 days ("did I delete the resume", "what did I do
        # today", "show my activity this week", ...). The history
        # already enforces a 30-day TTL so we just need to translate
        # the args into a query.
        if kind == "recall_history":
            return self._recall_history(args)

        if kind == "help":
            return ExecutionResult(self._help_text(), True, kind)

        if kind == "set_name":
            name = (args.get("name") or "").strip()
            if not name:
                return ExecutionResult("I didn't catch a name.", False, kind)
            self.memory.user_name = name
            return ExecutionResult(f"Got it. I'll call you {name} from now on.",
                                   True, kind)

        if kind == "get_name":
            name = self.memory.user_name
            if name:
                return ExecutionResult(f"Your name is {name}.", True, kind)
            return ExecutionResult(
                "I don't know your name yet. Tell me by saying 'my name is ...'",
                False, kind,
            )

        if kind == "reset_permissions":
            self.permissions.reset()
            return ExecutionResult("All permissions reset.", True, kind)

        if kind == "list_apps":
            apps = self.apps.known_apps()
            preview = ", ".join(apps[:15])
            more = f", and {len(apps) - 15} more" if len(apps) > 15 else ""
            return ExecutionResult(
                f"I can launch {len(apps)} apps including: {preview}{more}.",
                True, kind,
            )

        # Friendly small-talk intents (no permissions needed) -----------
        if kind == "greet":
            name = self.memory.user_name
            return ExecutionResult(
                f"Hello{', ' + name if name else ''}. How can I help?",
                True, kind,
            )
        if kind == "thanks":
            return ExecutionResult(
                "You're very welcome.", True, kind,
            )
        if kind == "how_are_you":
            return ExecutionResult(
                "Operating at full capacity. Ready when you are.",
                True, kind,
            )
        if kind == "who_are_you":
            return ExecutionResult(
                "I am JARVIS, your desktop assistant. Say 'help' for a quick tour.",
                True, kind,
            )

        # Window control --------------------------------------------------
        if kind == "show_gui":
            shown = bool(self.on_show_gui())
            msg = ("Bringing the dashboard up." if shown
                   else "I don't have a window to show.")
            return ExecutionResult(msg, shown, kind)
        if kind == "hide_gui":
            hidden = bool(self.on_hide_gui())
            msg = ("Going to the background. Say 'show UI' when you need me."
                   if hidden else "No window is currently visible.")
            return ExecutionResult(msg, hidden, kind)

        # File ops ------------------------------------------------------
        if kind == "open_folder":
            self.permissions.ensure(PermissionCategory.FILE_READ)
            r = self.files.open_folder(args["path"])
            self._remember_payload_path(r)
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "list_dir":
            self.permissions.ensure(PermissionCategory.FILE_READ)
            r = self.files.list_directory(args["path"])
            self._remember_path_arg(args.get("path"))
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "search_file":
            self.permissions.ensure(PermissionCategory.FILE_READ)
            r = self.files.search_files(args["pattern"], roots=args.get("roots"))
            return ExecutionResult(r.message, r.ok, kind)

        # Pronoun-style fallbacks: resolve "open it" / "list that folder"
        # to the most recently created or opened folder.
        if kind == "open_last_path":
            target = self.memory.last_path
            if not target:
                return ExecutionResult(
                    "I don't have a recent folder to open yet. "
                    "Try 'create folder Demo in Documents' first.",
                    False, kind,
                )
            self.permissions.ensure(PermissionCategory.FILE_READ)
            r = self.files.open_folder(target)
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "list_last_path":
            target = self.memory.last_path
            if not target:
                return ExecutionResult(
                    "No recent folder to list yet.", False, kind,
                )
            self.permissions.ensure(PermissionCategory.FILE_READ)
            r = self.files.list_directory(target)
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "create_folder":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = self.files.create_folder(args["parent"], args["name"])
            self._remember_payload_path(r)
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "create_file":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = self.files.create_file(
                args["parent"], args["name"], args.get("contents", "")
            )
            # Remember the *parent* folder so "open it" navigates there.
            if r.ok and r.payload is not None:
                try:
                    self.memory.remember_path(str(r.payload).rsplit("\\", 1)[0]
                                              .rsplit("/", 1)[0])
                except Exception:  # noqa: BLE001
                    pass
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "rename":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = self.files.rename(args["src"], args["new_name"])
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "copy":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = self.files.copy(args["src"], args["dest_dir"])
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "move":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = self.files.move(args["src"], args["dest_dir"])
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "delete":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = self.files.delete(args["path"])
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "open_cmd":
            self.permissions.ensure(PermissionCategory.APP_LAUNCH)
            r = self.files.open_cmd(args.get("path") or "home")
            return ExecutionResult(r.message, r.ok, kind)

        # Apps ----------------------------------------------------------
        if kind == "launch_app":
            name = (args.get("name") or "").strip()
            if not name or name.lower() in {"app", "application", "this", "that"}:
                return ExecutionResult("Which application should I launch?", False, kind)
            if not self.apps.is_known(name):
                return ExecutionResult(
                    f"'{name}' isn't on my whitelist. "
                    "You can add it to data/extra_apps.json.",
                    False, kind,
                )
            self.permissions.ensure(PermissionCategory.APP_LAUNCH)
            r = self.apps.launch(name)
            return ExecutionResult(r.message, r.ok, kind)

        # Web -----------------------------------------------------------
        if kind == "open_url":
            self.permissions.ensure(PermissionCategory.APP_LAUNCH)
            r = util_actions.open_url(args.get("url", ""))
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "web_search":
            self.permissions.ensure(PermissionCategory.APP_LAUNCH)
            r = util_actions.web_search(args.get("query", ""))
            return ExecutionResult(r.message, r.ok, kind)

        # System control ------------------------------------------------
        if kind == "shutdown":
            self.permissions.ensure(PermissionCategory.SYSTEM_CONTROL)
            r = self.system.shutdown()
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "restart":
            self.permissions.ensure(PermissionCategory.SYSTEM_CONTROL)
            r = self.system.restart()
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "sleep":
            self.permissions.ensure(PermissionCategory.SYSTEM_CONTROL)
            r = self.system.sleep()
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "lock":
            self.permissions.ensure(PermissionCategory.SYSTEM_CONTROL)
            r = self.system.lock()
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "log_off":
            self.permissions.ensure(PermissionCategory.SYSTEM_CONTROL)
            r = self.system.log_off()
            return ExecutionResult(r.message, r.ok, kind)

        if kind == "cancel_shutdown":
            r = self.system.cancel_pending_shutdown()
            return ExecutionResult(r.message, r.ok, kind)

        # Utilities (no permission needed — pure read of system state) --
        if kind == "get_time":
            r = util_actions.get_time()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "get_date":
            r = util_actions.get_date()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "tell_joke":
            r = util_actions.tell_joke()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "calculate":
            r = util_actions.calculate(args.get("expression", ""))
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "system_info":
            r = util_actions.system_info()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "screenshot":
            self.permissions.ensure(PermissionCategory.FILE_WRITE)
            r = util_actions.screenshot()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "volume_up":
            r = util_actions.volume_up()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "volume_down":
            r = util_actions.volume_down()
            return ExecutionResult(r.message, r.ok, kind)
        if kind == "volume_mute":
            r = util_actions.volume_mute()
            return ExecutionResult(r.message, r.ok, kind)

        # Scheduling ----------------------------------------------------
        if kind == "schedule_then":
            return self._schedule_then(args)

        if kind == "schedule_until_process":
            return self._schedule_until_process(args)

        # Fallback ------------------------------------------------------
        return ExecutionResult(
            "I'm not sure how to do that yet. Say 'help' to hear what I support.",
            False, "unknown",
        )

    # ------------------------------------------------------------------
    # Recent-paths helpers (powers "open it" / "go there")
    # ------------------------------------------------------------------
    def _remember_payload_path(self, op_result) -> None:
        """Push a successful FileOpResult.payload onto the recent ring."""
        if not op_result or not getattr(op_result, "ok", False):
            return
        payload = getattr(op_result, "payload", None)
        if payload is None:
            return
        try:
            self.memory.remember_path(str(payload))
        except Exception as exc:  # noqa: BLE001
            _log.debug("remember_path failed: %s", exc)

    def _remember_path_arg(self, raw_path) -> None:
        """For ops where we only have the requested path string."""
        if not raw_path:
            return
        try:
            from system.file_manager import normalise_path  # noqa: PLC0415

            self.memory.remember_path(str(normalise_path(str(raw_path))))
        except Exception as exc:  # noqa: BLE001
            _log.debug("remember_path_arg failed: %s", exc)

    # ------------------------------------------------------------------
    # Scheduler helpers
    # ------------------------------------------------------------------
    def _schedule_then(self, args: dict) -> ExecutionResult:
        seconds = int(args.get("seconds") or 0)
        then_text = (args.get("then") or "").strip()
        if seconds <= 0:
            return ExecutionResult(
                "I didn't catch a delay. Try 'wait 10 minutes then ...'.",
                False, "schedule_then",
            )
        if not then_text:
            return ExecutionResult(
                "I need to know what to do after the wait.", False, "schedule_then",
            )

        followup = self.parser.parse(then_text)
        if followup.kind == "unknown":
            return ExecutionResult(
                f"I understood the wait, but not the action: '{then_text}'.",
                False, "schedule_then",
            )

        def _action() -> None:
            _log.info("Scheduled task firing -> %s", followup)
            try:
                self._dispatch(followup)
            except Exception as exc:  # noqa: BLE001
                _log.error("Scheduled action failed: %s", exc)

        task = self.scheduler.schedule_after(
            seconds, _action, description=f"After {seconds}s: {then_text}"
        )
        return ExecutionResult(
            f"Okay. In {seconds} seconds I'll: {then_text} (task #{task.id}).",
            True, "schedule_then",
        )

    def _schedule_until_process(self, args: dict) -> ExecutionResult:
        process = (args.get("process") or "").strip()
        then_text = (args.get("then") or "").strip()
        if not process or not then_text:
            return ExecutionResult(
                "Try: 'wait until chrome.exe ends, then shut down'.",
                False, "schedule_until_process",
            )

        followup = self.parser.parse(then_text)
        if followup.kind == "unknown":
            return ExecutionResult(
                f"I understood the wait, but not the action: '{then_text}'.",
                False, "schedule_until_process",
            )

        def _action() -> None:
            _log.info("Process %s ended — running %s", process, followup)
            try:
                self._dispatch(followup)
            except Exception as exc:  # noqa: BLE001
                _log.error("Scheduled action failed: %s", exc)

        task = self.scheduler.schedule_when_process_ends(
            process, _action,
            description=f"When {process} ends: {then_text}",
        )
        return ExecutionResult(
            f"Watching for {process} to end, then I'll: {then_text} (task #{task.id}).",
            True, "schedule_until_process",
        )

    # ------------------------------------------------------------------
    # History recall ("did I delete X yesterday?")
    # ------------------------------------------------------------------
    def _recall_history(self, args: Dict[str, Any]) -> ExecutionResult:
        """Answer a question about past activity within the 30-day window."""
        days = int(args.get("days") or RETENTION_DAYS)
        days = max(1, min(RETENTION_DAYS, days))
        keyword = (args.get("keyword") or "").strip() or None
        intent_kind = (args.get("intent_kind") or "").strip() or None

        matches = self.history.find(
            keyword=keyword, intent_kind=intent_kind, days=days,
        )
        scope = _describe_window(days)

        if not matches:
            qualifiers: list[str] = []
            if keyword:
                qualifiers.append(f"matching '{keyword}'")
            if intent_kind:
                qualifiers.append(f"of type '{intent_kind}'")
            tail = (" " + " ".join(qualifiers)) if qualifiers else ""
            return ExecutionResult(
                f"No, I don't see any commands{tail} from {scope}.",
                True, "recall_history",
            )

        # `find` already returns newest-first.
        headline = matches[0]
        when_pretty = _humanise_when(headline)
        what = (headline.user_text.strip()
                or headline.response.strip()
                or headline.intent_kind)
        if len(what) > 80:
            what = what[:77].rstrip() + "..."

        msg = f"Yes — most recently you {what} ({when_pretty})."
        if len(matches) > 1:
            msg += (
                f" I see {len(matches)} matching command"
                f"{'s' if len(matches) != 1 else ''} from {scope}."
            )
        return ExecutionResult(msg, True, "recall_history")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    @staticmethod
    def _help_text() -> str:
        return (
            "Here are some things you can ask me. "
            "Open my Documents folder. "
            "Create a folder named Projects in Documents. "
            "Create a file notes.txt with this text: hello. "
            "Rename, copy, or move files between folders. "
            "Launch Chrome, Notepad, or any whitelisted app. "
            "Open a website, search the web, or take a screenshot. "
            "Tell me a joke, the time, the date, or do quick math. "
            "Read system info, change volume, or mute. "
            "Ask 'did I delete that file yesterday' to recall recent activity. "
            "Wait ten minutes then restart the PC. "
            "Reset permissions. Or say goodbye to exit."
        )


# ---------------------------------------------------------------------------
# Free helpers used by the recall handler
# ---------------------------------------------------------------------------
def _describe_window(days: int) -> str:
    """Map a day count back to a natural-language window description."""
    if days <= 1:
        return "today"
    if days == 2:
        return "today or yesterday"
    if days == 7:
        return "the last week"
    if days == 30:
        return "the last 30 days"
    return f"the last {days} days"


def _humanise_when(entry: HistoryEntry) -> str:
    """Render an entry's timestamp as 'today at 3:14 PM' / '3 days ago'."""
    from datetime import datetime  # noqa: PLC0415

    local = entry_local_dt(entry)
    if local is None:
        return entry.timestamp or "an unknown time"
    today = datetime.now().astimezone().date()
    delta_days = (today - local.date()).days
    time_str = local.strftime("%I:%M %p").lstrip("0")
    if delta_days == 0:
        return f"today at {time_str}"
    if delta_days == 1:
        return f"yesterday at {time_str}"
    if delta_days < 7:
        return f"{delta_days} days ago at {time_str}"
    return local.strftime("on %b %d at ") + time_str
