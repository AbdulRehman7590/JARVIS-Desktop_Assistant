"""JARVIS — entry point.

Usage:

    python main.py                # GUI dashboard (the default)
    python main.py --text-only    # type commands instead of speaking
    python main.py --voice-only   # background listener with no window
    python main.py --wake         # require "hey jarvis" before each command (voice modes)
    python main.py --no-llm       # don't consult the LLM even if an API key exists

The :class:`JarvisApp` class wires together every module so it can be reused
by both the CLI and GUI front-ends.
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

from core.command_parser import CommandParser
from core.env_manager import load_env, write_example
from core.executor import Executor, ExecutionResult
from core.history import History
from core.llm_client import LLMClient
from core.memory import Memory
from core.permissions import PermissionCategory, PermissionManager
from speech.text_to_speech import Speaker
from system.app_launcher import AppLauncher
from system.file_manager import FileManager
from system.system_control import SystemController
from utils.logger import get_logger
from utils.paths import ensure_dirs
from utils.scheduler import Scheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_name_input(raw: str) -> str:
    """Strip common conversational prefixes from a user-supplied name.

    Examples:
        ``"my name is Tony"`` -> ``"Tony"``
        ``"  i'm Bruce  "``   -> ``"Bruce"``
        ``"Tony"``            -> ``"Tony"``
    """
    if not raw:
        return ""
    text = raw.strip().strip(".!?,").strip()
    lowered = text.lower()
    for prefix in ("my name is ", "i am ", "i'm ", "im ", "call me ",
                   "this is ", "name is "):
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    first = text.split()[0] if text.split() else ""
    return first.strip(".!?,")


# ---------------------------------------------------------------------------
# JarvisApp — composition root
# ---------------------------------------------------------------------------
class JarvisApp:
    """Owns every long-lived service and provides ``handle(text)``."""

    def __init__(
        self,
        speaker: Optional[Speaker] = None,
        permission_prompter: Optional[Callable[[PermissionCategory], str]] = None,
        confirmer: Optional[Callable[[str], bool]] = None,
        enable_llm: bool = True,
    ) -> None:
        ensure_dirs()
        write_example()
        load_env()  # populates os.environ from .env (if present)

        self.log = get_logger()

        self.speaker = speaker or Speaker()
        self.memory = Memory()
        self.history = History()
        self.scheduler = Scheduler()

        # Default to CLI prompts if the front-end didn't provide its own.
        self._permission_prompter = permission_prompter or self._cli_permission_prompt
        self._confirmer = confirmer or self._cli_confirm

        self.permissions = PermissionManager(prompter=self._permission_prompter)
        self.files = FileManager(confirmer=self._confirmer)
        self.apps = AppLauncher()
        self.system = SystemController(confirmer=self._confirmer)

        self.llm = LLMClient() if enable_llm else None

        self.executor = Executor(
            memory=self.memory,
            permissions=self.permissions,
            file_manager=self.files,
            app_launcher=self.apps,
            system_controller=self.system,
            scheduler=self.scheduler,
            history=self.history,
            parser=CommandParser(),
            llm=self.llm,
        )

        if self.llm and self.llm.is_configured:
            self.log.info("LLM brain enabled: %s", self.llm.status())
        else:
            self.log.info("LLM brain disabled (rule-based parsing only).")

    # ------------------------------------------------------------------
    # Default CLI prompters (overridable by GUI / voice front-ends)
    # ------------------------------------------------------------------
    @staticmethod
    def _cli_permission_prompt(category: PermissionCategory) -> str:
        try:
            return input(
                f"\n[Permission needed] Allow JARVIS to {category.description}? "
                "(yes / no / always / never): "
            )
        except EOFError:
            return "no"

    @staticmethod
    def _cli_confirm(question: str) -> bool:
        try:
            return input(f"\n[Confirm] {question} (yes/no): ").strip().lower() in {
                "y", "yes"
            }
        except EOFError:
            return False

    # ------------------------------------------------------------------
    # Public façade
    # ------------------------------------------------------------------
    def speak(self, text: str) -> None:
        self.speaker.speak(text)

    def handle(self, user_text: str) -> ExecutionResult:
        """Handle one command end-to-end and speak the response."""
        result = self.executor.handle(user_text)
        if result.response:
            self.speak(result.response)
        return result

    def first_run_setup(self) -> None:
        """Ask for the user's name on first launch (CLI default)."""
        if self.memory.has_user_name():
            self.speak(f"Welcome back, {self.memory.user_name}.")
            return
        self.speak("Hello. I am JARVIS. What is your name?")
        try:
            raw = input("Your name: ").strip()
        except EOFError:
            raw = ""
        name = clean_name_input(raw)
        if name:
            self.memory.user_name = name
            self.speak(f"Pleased to meet you, {name}.")
        else:
            self.speak("No problem. You can tell me your name later.")

    def shutdown(self) -> None:
        try:
            self.speaker.shutdown()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------
def run_text_mode(app: JarvisApp) -> None:
    """Loop reading commands from stdin (great for development & headless)."""
    app.first_run_setup()
    app.speak("Text mode ready. Type a command, or 'exit' to quit.")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        result = app.handle(line)
        if result.should_exit:
            break
    app.shutdown()


def run_voice_only_mode(app: JarvisApp, wake_word: bool = False) -> None:
    """Background voice listener — no window, no terminal interaction needed."""
    from speech.speech_to_text import Listener

    listener = Listener()
    if not listener.has_microphone:
        app.speak("No microphone available. Falling back to text mode.")
        run_text_mode(app)
        return

    # ----- voice-aware permission + confirmation prompts -----
    def _voice_permission_prompt(category: PermissionCategory) -> str:
        app.speak(
            f"Permission needed. Allow me to {category.description}? "
            "Say yes, no, always, or never."
        )
        for _ in range(2):
            heard = listener.listen_once(timeout=8.0) or ""
            heard = heard.lower().strip(" .!?,")
            if "always" in heard:
                return "always"
            if "never" in heard:
                return "never"
            if heard.startswith(("yes", "yeah", "yep", "sure", "ok", "okay")):
                return "yes"
            if heard.startswith(("no", "nope", "nah", "cancel")):
                return "no"
            app.speak("I didn't catch that. Yes, no, always, or never?")
        return "no"

    def _voice_confirm(question: str) -> bool:
        from speech.speech_to_text import classify_yes_no  # noqa: PLC0415

        app.speak(question)
        for _ in range(3):
            heard = listener.listen_once(timeout=8.0) or ""
            verdict = classify_yes_no(heard)
            if verdict is True:
                return True
            if verdict is False:
                return False
            app.speak("Sorry, please say yes or no.")
        return False

    # Re-wire JarvisApp's prompters now that the listener exists.
    app._permission_prompter = _voice_permission_prompt
    app._confirmer = _voice_confirm
    app.permissions._prompter = _voice_permission_prompt  # type: ignore[attr-defined]
    app.files._confirm = _voice_confirm                   # type: ignore[attr-defined]
    app.system._confirm = _voice_confirm                  # type: ignore[attr-defined]

    # ----- first-run name prompt over voice -----
    if not app.memory.has_user_name():
        app.speak("Hello. I am JARVIS. What is your name?")
        for _ in range(3):
            heard = listener.listen_once(timeout=8.0)
            cleaned = clean_name_input(heard or "")
            if cleaned:
                app.memory.user_name = cleaned
                app.speak(f"Pleased to meet you, {cleaned}.")
                break
            app.speak("Sorry, I didn't catch that. What's your name?")
        else:
            app.speak("No problem. You can tell me your name later.")
    else:
        app.speak(f"Welcome back, {app.memory.user_name}.")

    if wake_word:
        app.speak("Voice-only mode. Say 'hey JARVIS' to begin a command.")

    while True:
        if wake_word:
            if not listener.wait_for_wake_word(timeout=None):
                continue
            app.speak("Yes?")

        text = listener.listen_once(timeout=10.0)
        if not text:
            if not wake_word:
                continue
            app.speak("I didn't hear anything.")
            continue

        result = app.handle(text)
        if result.should_exit:
            break

    app.shutdown()


def run_gui_mode(
    app: JarvisApp,
    voice: bool = True,
    wake_word: bool = True,
    start_hidden: bool = False,
) -> None:
    """Launch the PySide6 dashboard.

    When ``start_hidden=True`` the window is hidden immediately and only
    the floating overlay + tray icon are visible. Useful for
    "background" mode — recover the window with the voice command
    "show UI" or by clicking the tray icon.
    """
    from gui.qt_dashboard import run_qt_dashboard  # noqa: PLC0415

    run_qt_dashboard(
        app=app, voice=voice,
        wake_word=wake_word, start_hidden=start_hidden,
    )
    app.shutdown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jarvis",
        description="Voice-controlled desktop assistant inspired by JARVIS. "
                    "Default mode is the GUI dashboard.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--text-only", action="store_true",
                      help="Type commands instead of speaking them (no GUI).")
    mode.add_argument("--voice-only", action="store_true",
                      help="Background voice listener with no window.")
    mode.add_argument("--gui", action="store_true",
                      help="Force the GUI dashboard (this is the default).")
    p.add_argument("--no-mic", action="store_true",
                   help="GUI mode: disable microphone listening.")
    p.add_argument(
        "--no-wake", action="store_true",
        help="Voice modes: stream every utterance as a command without "
             "requiring 'hey jarvis' first. (Default: wake word IS required.)",
    )
    p.add_argument("--no-llm", action="store_true",
                   help="Don't use the LLM even if an API key is configured.")
    p.add_argument(
        "--hidden", "--background", action="store_true", dest="hidden",
        help="GUI mode: start with the window hidden (background mode). "
             "Use the system-tray icon, the floating overlay, or the "
             "voice command 'show UI' to bring the window back.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    log = get_logger()

    chosen_mode = ("text" if args.text_only
                   else "voice" if args.voice_only
                   else "gui")
    wake = not args.no_wake
    log.info("Starting JARVIS (mode=%s, wake=%s, llm=%s)",
             chosen_mode, wake, not args.no_llm)

    app = JarvisApp(enable_llm=not args.no_llm)

    try:
        if chosen_mode == "text":
            run_text_mode(app)
        elif chosen_mode == "voice":
            run_voice_only_mode(app, wake_word=wake)
        else:
            run_gui_mode(
                app, voice=not args.no_mic, wake_word=wake,
                start_hidden=args.hidden,
            )
    except KeyboardInterrupt:
        print()
        app.speak("Interrupted. Goodbye.")
    except Exception as exc:  # noqa: BLE001 - top-level guard
        log.exception("Fatal error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
