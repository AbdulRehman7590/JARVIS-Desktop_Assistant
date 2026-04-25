"""Natural-language → Intent parser.

This is intentionally pattern-based (regex + keyword lookup) rather than ML-
based: it's deterministic, fast, requires no model download, and is easy to
extend. Each :class:`IntentRule` returns an :class:`Intent` if it matches.

Supported intents (subset, see ``Intent.kind``):

================================  ================================
 Intent kind                       Example phrasing
================================  ================================
 ``set_name``                      "my name is Tony"
 ``get_name``                      "what is my name", "who am i"
 ``open_folder``                   "open my Documents folder"
 ``list_dir``                      "list files in Downloads"
 ``search_file``                   "search for resume.pdf"
 ``create_folder``                 "create a folder named Projects in Documents"
 ``create_file``                   "create file notes.txt with this text: ..."
 ``rename``                        "rename file old.txt to new.txt"
 ``delete``                        "delete folder ..."
 ``open_cmd``                      "open cmd in Documents"
 ``launch_app``                    "launch chrome"
 ``shutdown``/``restart``/``sleep``/``lock``
 ``cancel_shutdown``               "cancel shutdown"
 ``schedule_then``                 "wait 10 minutes then restart the PC"
 ``schedule_until_process``        "wait until chrome.exe ends, then ..."
 ``reset_permissions``             "reset permissions"
 ``list_apps``                     "what apps can you launch"
 ``help``                          "help", "what can you do"
 ``exit``                          "exit", "quit", "goodbye"
 ``unknown``                       — fallback
================================  ================================
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Intent dataclass
# ---------------------------------------------------------------------------
@dataclass
class Intent:
    """Structured representation of a parsed user command."""

    kind: str
    args: Dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"Intent({self.kind!r}, {self.args!r})"


# ---------------------------------------------------------------------------
# Number-word handling — keeps the parser readable.
# ---------------------------------------------------------------------------
_NUMBER_WORDS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90,
    "a": 1, "an": 1,
}


def _word_to_int(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


_TIME_UNITS: Dict[str, int] = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}


def _parse_duration_seconds(text: str) -> Optional[int]:
    """Extract durations like ``"10 minutes"``, ``"two hours"``, ``"30 sec"``."""
    if not text:
        return None
    m = re.search(
        r"\b(\d+|" + "|".join(_NUMBER_WORDS.keys()) + r")\s+"
        r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
        text.lower(),
    )
    if not m:
        return None
    n = _word_to_int(m.group(1))
    unit = _TIME_UNITS.get(m.group(2))
    if n is None or unit is None:
        return None
    return n * unit


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------
RuleFn = Callable[[str], Optional[Intent]]


@dataclass
class IntentRule:
    """A regex-or-keyword rule that produces an :class:`Intent` if matched."""

    name: str
    matcher: RuleFn

    def __call__(self, text: str) -> Optional[Intent]:
        return self.matcher(text)


def _strip(text: str) -> str:
    """Collapse whitespace and trim trailing punctuation, lower-case."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = cleaned.strip(" .!?,;:")
    return cleaned


def _make_regex_rule(
    name: str,
    pattern: str,
    builder: Callable[[re.Match[str], str], Intent],
    flags: int = re.IGNORECASE,
) -> IntentRule:
    compiled = re.compile(pattern, flags)

    def _matcher(text: str) -> Optional[Intent]:
        m = compiled.search(text)
        if not m:
            return None
        intent = builder(m, text)
        intent.raw = text
        return intent

    return IntentRule(name=name, matcher=_matcher)


# ---------------------------------------------------------------------------
# Builders for individual rules — kept tiny on purpose.
# ---------------------------------------------------------------------------
def _build_rules() -> List[IntentRule]:
    rules: List[IntentRule] = []

    # ----- exit / help / identity ----------------------------------------
    rules.append(_make_regex_rule(
        "exit",
        r"^\s*(?:exit|quit|bye|goodbye|stop listening|shut up)\s*$",
        lambda m, t: Intent("exit"),
    ))
    rules.append(_make_regex_rule(
        "help",
        r"\b(help|what can you do|commands)\b",
        lambda m, t: Intent("help"),
    ))

    # ----- window control (background / foreground) ---------------------
    rules.append(_make_regex_rule(
        "show_gui",
        r"\b("
        r"show\s+(?:the\s+)?(?:ui|gui|app|window|dashboard|interface)"
        r"|open\s+(?:the\s+)?(?:ui|gui|dashboard|interface|window)"
        r"|bring\s+(?:up|back)\s+(?:the\s+)?(?:ui|window|dashboard)"
        r"|come\s+(?:back|out|forward)"
        r"|wake\s+up"
        r"|maximi[sz]e"
        r")\b",
        lambda m, t: Intent("show_gui"),
    ))
    rules.append(_make_regex_rule(
        "hide_gui",
        r"\b("
        r"hide\s+(?:the\s+)?(?:ui|gui|app|window|dashboard|interface|yourself)"
        r"|close\s+(?:the\s+)?(?:ui|gui|window|dashboard|interface)"
        r"|minimi[sz]e\s+(?:the\s+)?(?:ui|gui|window|dashboard|interface|yourself|app)?"
        r"|go\s+(?:to\s+)?(?:the\s+)?background"
        r"|background\s+mode"
        r"|disappear"
        r")\b",
        lambda m, t: Intent("hide_gui"),
    ))
    rules.append(_make_regex_rule(
        "set_name",
        r"\b(?:my name is|call me|i am|i'm)\s+([A-Za-z][A-Za-z\-' ]{0,40})",
        lambda m, t: Intent("set_name", {"name": m.group(1).strip()}),
    ))
    rules.append(_make_regex_rule(
        "get_name",
        r"\b(what(?:'s| is) my name|who am i|do you know my name)\b",
        lambda m, t: Intent("get_name"),
    ))

    # ----- permissions ----------------------------------------------------
    rules.append(_make_regex_rule(
        "reset_permissions",
        r"\b(reset|clear|forget)\b.*\bpermissions?\b",
        lambda m, t: Intent("reset_permissions"),
    ))
    rules.append(_make_regex_rule(
        "list_apps",
        r"\b(?:what|which)\s+apps?\b.*\b(?:launch|open|run|start)\b",
        lambda m, t: Intent("list_apps"),
    ))

    # ----- scheduling ----------------------------------------------------
    rules.append(_make_regex_rule(
        "schedule_until_process",
        r"\bwait\s+(?:un)?til\s+(?:process\s+)?([\w.\-]+)\s+(?:ends?|closes?|exits?|finishes?|stops?)"
        r"(?:[\s,]+then\s+)?(.*)",
        lambda m, t: Intent(
            "schedule_until_process",
            {"process": m.group(1).strip(), "then": _strip(m.group(2))},
        ),
    ))

    def _schedule_then_builder(m: re.Match[str], t: str) -> Intent:
        seconds = _parse_duration_seconds(t)
        return Intent(
            "schedule_then",
            {"seconds": seconds or 0, "then": _strip(m.group(1))},
        )

    rules.append(_make_regex_rule(
        "schedule_then",
        r"\bwait\b.*?(?:then|and then|after that)\s+(.+)$",
        _schedule_then_builder,
    ))

    # ----- system control ------------------------------------------------
    rules.append(_make_regex_rule(
        "cancel_shutdown",
        r"\b(cancel|abort)\b.*\bshutdown\b",
        lambda m, t: Intent("cancel_shutdown"),
    ))
    rules.append(_make_regex_rule(
        "shutdown",
        r"\b(shut\s*down|power\s*off|turn\s*off)\b.*\b(pc|computer|system|machine)?\b",
        lambda m, t: Intent("shutdown"),
    ))
    rules.append(_make_regex_rule(
        "restart",
        r"\b(restart|reboot)\b.*\b(pc|computer|system|machine)?\b",
        lambda m, t: Intent("restart"),
    ))
    rules.append(_make_regex_rule(
        "sleep",
        r"\b(sleep|hibernate|suspend)\b.*\b(pc|computer)?\b",
        lambda m, t: Intent("sleep"),
    ))
    rules.append(_make_regex_rule(
        "lock",
        r"\block\b.*\b(pc|computer|workstation|screen)\b",
        lambda m, t: Intent("lock"),
    ))
    rules.append(_make_regex_rule(
        "log_off",
        r"\b(log\s*off|sign\s*out)\b",
        lambda m, t: Intent("log_off"),
    ))

    # ----- file ops: write group ----------------------------------------
    # "create a folder named X in Y"
    rules.append(_make_regex_rule(
        "create_folder",
        r"\b(?:create|make|new)\b\s+(?:a\s+)?(?:folder|directory)\s+"
        r"(?:named|called)?\s*['\"]?([\w\-. ]+?)['\"]?"
        r"(?:\s+(?:in|inside|under|at)\s+(.+))?$",
        lambda m, t: Intent(
            "create_folder",
            {"name": m.group(1).strip(),
             "parent": (m.group(2) or "documents").strip()},
        ),
    ))

    # "create a file notes.txt with this text: hello world"
    # also "create a file named foo.md in Desktop with content ..."
    # Note: longest alternatives first — Python regex picks the first match
    # of the alternation, so order matters.
    rules.append(_make_regex_rule(
        "create_file_with_text",
        r"\b(?:create|make|write|new)\b\s+(?:a\s+)?file\s+"
        r"(?:named|called)?\s*['\"]?([\w\-. ]+?)['\"]?"
        r"(?:\s+(?:in|inside|under|at)\s+([^,]+?))?"
        r"\s+(?:with\s+(?:this\s+)?(?:the\s+)?(?:text|content|contents)"
        r"|that\s+says|containing|with)"
        r"\s*[:\-]?\s*(.+)$",
        lambda m, t: Intent(
            "create_file",
            {
                "name": m.group(1).strip(),
                "parent": (m.group(2) or "documents").strip(),
                "contents": m.group(3).strip().strip('"').strip("'"),
            },
        ),
    ))

    # "create a file notes.txt in Documents" (no contents)
    rules.append(_make_regex_rule(
        "create_file_empty",
        r"\b(?:create|make|new)\b\s+(?:an?\s+)?(?:empty\s+)?file\s+"
        r"(?:named|called)?\s*['\"]?([\w\-. ]+?)['\"]?"
        r"(?:\s+(?:in|inside|under|at)\s+(.+))?$",
        lambda m, t: Intent(
            "create_file",
            {
                "name": m.group(1).strip(),
                "parent": (m.group(2) or "documents").strip(),
                "contents": "",
            },
        ),
    ))

    # "rename file old.txt to new.txt"
    rules.append(_make_regex_rule(
        "rename",
        r"\brename\b\s+(?:file|folder|the\s+file|the\s+folder)?\s*"
        r"['\"]?([\w\-. /\\:]+?)['\"]?\s+(?:to|as)\s+['\"]?([\w\-. ]+?)['\"]?\s*$",
        lambda m, t: Intent(
            "rename",
            {"src": m.group(1).strip(), "new_name": m.group(2).strip()},
        ),
    ))

    rules.append(_make_regex_rule(
        "delete",
        r"\bdelete\b\s+(?:the\s+)?(?:file|folder|directory)?\s*['\"]?([\w\-. /\\:]+?)['\"]?\s*$",
        lambda m, t: Intent("delete", {"path": m.group(1).strip()}),
    ))

    # "copy file foo.txt to Documents" / "copy D:\a\b.txt into Desktop"
    rules.append(_make_regex_rule(
        "copy",
        r"\bcopy\b\s+(?:the\s+)?(?:file|folder)?\s*['\"]?([\w\-. /\\:]+?)['\"]?\s+"
        r"(?:to|into|in)\s+(.+)$",
        lambda m, t: Intent(
            "copy",
            {"src": m.group(1).strip(), "dest_dir": m.group(2).strip()},
        ),
    ))

    # "move file foo.txt to Documents"
    rules.append(_make_regex_rule(
        "move",
        r"\bmove\b\s+(?:the\s+)?(?:file|folder)?\s*['\"]?([\w\-. /\\:]+?)['\"]?\s+"
        r"(?:to|into|in)\s+(.+)$",
        lambda m, t: Intent(
            "move",
            {"src": m.group(1).strip(), "dest_dir": m.group(2).strip()},
        ),
    ))

    # ----- file ops: read group -----------------------------------------
    # Pronoun resolution: "open it", "go there", "show that folder", etc.
    # These resolve to the *most recently created or touched* path that
    # JARVIS remembers in `Memory.last_path`.
    rules.append(_make_regex_rule(
        "open_last_path",
        r"^\s*(?:please\s+)?(?:"
        r"open\s+(?:it|that|that\s+(?:one|folder|directory)|the\s+(?:last|new|previous|recent)\s+(?:one|folder|directory))"
        r"|(?:go|take\s+me|jump|cd)\s+(?:there|to\s+(?:it|that|that\s+folder))"
        r"|show\s+(?:it|me\s+(?:it|that(?:\s+folder)?))"
        r"|where\s+(?:is\s+)?(?:it|that(?:\s+folder)?)"
        r")\s*[.!?]*\s*$",
        lambda m, t: Intent("open_last_path"),
    ))
    rules.append(_make_regex_rule(
        "list_last_path",
        r"^\s*(?:please\s+)?(?:"
        r"list\s+(?:it|that|that\s+(?:one|folder|directory))"
        r"|what(?:'s| is)\s+in\s+(?:it|there|that(?:\s+folder)?)"
        r")\s*[.!?]*\s*$",
        lambda m, t: Intent("list_last_path"),
    ))

    rules.append(_make_regex_rule(
        "open_cmd",
        r"\bopen\b\s+(?:cmd|command\s*prompt|terminal|shell|powershell)"
        r"(?:\s+(?:in|here|at)\s*(.*))?$",
        lambda m, t: Intent(
            "open_cmd",
            {"path": (m.group(1) or "home").strip() or "home"},
        ),
    ))

    rules.append(_make_regex_rule(
        "list_dir",
        r"\b(?:list|show|read)\b\s+(?:files|contents|everything|directory)?"
        r"\s*(?:in|of|inside|from|under)?\s*(.+)$",
        lambda m, t: Intent("list_dir", {"path": m.group(1).strip()}),
    ))

    # search_file must NOT swallow "search the web for X" — that's handled by
    # the dedicated web_search rule lower down, but rules run in order so we
    # explicitly skip web/internet/google patterns here.
    def _search_file_matcher(text: str) -> Optional[Intent]:
        if re.search(r"\b(?:web|internet|google|online)\b", text, re.IGNORECASE):
            return None
        m = re.search(
            r"\b(?:search|find|look)\b\s+(?:for\s+)?(?:a\s+)?(?:file|folder|files)?"
            r"\s*(?:named|called)?\s*['\"]?([\w\-. *?]+?)['\"]?"
            r"(?:\s+(?:in|inside|under|on)\s+(.+))?$",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        intent = Intent(
            "search_file",
            {"pattern": m.group(1).strip(),
             "roots": [m.group(2).strip()] if m.group(2) else None},
        )
        intent.raw = text
        return intent

    rules.append(IntentRule(name="search_file", matcher=_search_file_matcher))

    # "open my Documents folder", "open Downloads"
    rules.append(_make_regex_rule(
        "open_folder",
        r"\bopen\b\s+(?:my\s+|the\s+)?(.+?)\s*(?:folder|directory)\s*$",
        lambda m, t: Intent("open_folder", {"path": m.group(1).strip()}),
    ))
    # "open D:\Stuff" (raw path)
    rules.append(_make_regex_rule(
        "open_folder_path",
        r"\bopen\b\s+([A-Za-z]:[\\/][^\s]+)\s*$",
        lambda m, t: Intent("open_folder", {"path": m.group(1).strip()}),
    ))

    # ----- web ----------------------------------------------------------
    # "open https://github.com" / "open google.com"
    rules.append(_make_regex_rule(
        "open_url",
        r"\bopen\b\s+(?:the\s+)?(?:url|link|website|site|page)?\s*"
        r"((?:https?://)?[\w\-]+(?:\.[\w\-]+)+(?:[/?#][^\s]*)?)\s*$",
        lambda m, t: Intent("open_url", {"url": m.group(1).strip()}),
    ))
    # "search the web for X" / "google how to ..."
    rules.append(_make_regex_rule(
        "web_search",
        r"\b(?:google|search\s+(?:the\s+)?(?:web|google|internet)\s+for)\s+(.+)$",
        lambda m, t: Intent("web_search", {"query": m.group(1).strip()}),
    ))

    # ----- time / date / info / utilities -------------------------------
    # ----- time -----
    rules.append(_make_regex_rule(
        "get_time",
        r"\b("
        r"what(?:'s| is| are)\s+(?:the\s+)?time(?:\s+(?:is\s+it|now|right\s+now))?"
        r"|what\s+time\s+(?:is\s+it|do\s+(?:we|i)\s+have)"
        r"|(?:tell|give|show)\s+me\s+(?:the\s+)?(?:current\s+)?time"
        r"|(?:current\s+|what'?s\s+the\s+)?time(?:\s+now)?\s*\??$"
        r"|do\s+you\s+(?:know|have)\s+the\s+time"
        r"|got\s+the\s+time"
        r")\b",
        lambda m, t: Intent("get_time"),
    ))

    # ----- date / day -----
    rules.append(_make_regex_rule(
        "get_date",
        r"\b("
        r"what(?:'s| is)\s+(?:the\s+|today'?s\s+)?(?:date|day)"
        r"|today(?:'s)?\s+(?:date|day)"
        r"|what\s+(?:day|date)\s+(?:is\s+(?:it|today)|today\s+is)"
        r"|(?:tell|give)\s+me\s+(?:the\s+|today'?s\s+)?(?:date|day)"
        r"|what\s+day\s+of\s+the\s+week"
        r"|date\s+today"
        r")\b",
        lambda m, t: Intent("get_date"),
    ))

    # ----- joke -----
    rules.append(_make_regex_rule(
        "tell_joke",
        r"\b("
        r"(?:tell|crack|say|share|give|hit\s+me\s+with)\s+(?:me\s+)?(?:a\s+|another\s+|some\s+)?joke"
        r"|make\s+me\s+laugh"
        r"|got\s+(?:any|a)\s+joke"
        r"|i\s+want\s+(?:a\s+)?joke"
        r"|something\s+funny"
        r")\b",
        lambda m, t: Intent("tell_joke"),
    ))

    # ----- system info -----
    rules.append(_make_regex_rule(
        "system_info",
        r"\b("
        r"system\s+info(?:rmation)?"
        r"|cpu\s+(?:usage|load|status)"
        r"|how\s+(?:much|is)\s+(?:my\s+)?(?:ram|memory)\s*(?:used|free|usage)?"
        r"|(?:pc|computer|machine)\s+(?:stats?|status|info|health)"
        r"|(?:check|show)\s+(?:my\s+)?(?:cpu|memory|ram|system)"
        r"|how\s+(?:is|am)\s+(?:my\s+)?(?:pc|computer|system)\s+(?:doing|running)"
        r")\b",
        lambda m, t: Intent("system_info"),
    ))

    # ----- screenshot -----
    rules.append(_make_regex_rule(
        "screenshot",
        r"\b("
        r"(?:take|capture|grab|snap|make)\s+(?:a\s+|the\s+|me\s+a\s+)?(?:screen\s*shot|screen\s*capture|snap)"
        r"|screen\s*shot\s+(?:please|now|this)?"
        r"|capture\s+(?:my\s+)?screen"
        r")\b",
        lambda m, t: Intent("screenshot"),
    ))

    # ----- greetings (no-op friendly chat handled by chip / LLM) -----
    rules.append(_make_regex_rule(
        "greet",
        r"^(?:hi|hello|hey|yo|good\s+(?:morning|afternoon|evening))\s*$",
        lambda m, t: Intent("greet"),
    ))

    # ----- thanks -----
    rules.append(_make_regex_rule(
        "thanks",
        r"^(?:thanks|thank\s+you|thx|cheers|ty)\s*[!.]*\s*$",
        lambda m, t: Intent("thanks"),
    ))

    # ----- how are you -----
    rules.append(_make_regex_rule(
        "how_are_you",
        r"\b(?:how\s+(?:are|r)\s+(?:you|u)|how(?:'s| is)\s+it\s+going|how\s+have\s+you\s+been|whats?\s+up)\b",
        lambda m, t: Intent("how_are_you"),
    ))

    # ----- who are you / what can you do already maps to help -----
    rules.append(_make_regex_rule(
        "who_are_you",
        r"\b(?:who\s+are\s+you|introduce\s+yourself|what\s+are\s+you)\b",
        lambda m, t: Intent("who_are_you"),
    ))

    # Volume
    rules.append(_make_regex_rule(
        "volume_up",
        r"\b(?:volume\s+up|increase\s+(?:the\s+)?volume|turn\s+(?:it\s+)?up|louder)\b",
        lambda m, t: Intent("volume_up"),
    ))
    rules.append(_make_regex_rule(
        "volume_down",
        r"\b(?:volume\s+down|decrease\s+(?:the\s+)?volume|turn\s+(?:it\s+)?down|quieter)\b",
        lambda m, t: Intent("volume_down"),
    ))
    rules.append(_make_regex_rule(
        "volume_mute",
        r"\b(?:mute|unmute|silence)\b",
        lambda m, t: Intent("volume_mute"),
    ))

    # "calculate 12 * 7" / "what is 5 plus 3"
    def _calc_normalise(expr: str) -> str:
        replacements = {
            " plus ": " + ", " minus ": " - ", " times ": " * ",
            " divided by ": " / ", " over ": " / ", " mod ": " % ",
            "^": "**",
        }
        result = expr
        for k, v in replacements.items():
            result = result.replace(k, v)
        return result

    # Only fire when the captured expression contains digits or math operators.
    # Otherwise "what is the capital of France" would be sent to the calculator.
    def _calc_builder(m: re.Match[str], t: str) -> Optional[Intent]:
        expr = _calc_normalise(m.group(1).strip())
        if not re.search(r"[0-9]", expr) or not re.search(
            r"[\+\-\*/%]|plus|minus|times|over|divided", expr.lower()
        ):
            return None
        return Intent("calculate", {"expression": expr})

    def _calc_matcher(text: str) -> Optional[Intent]:
        m = re.search(
            r"\b(?:calculate|compute|what(?:'s| is))\s+(.+?)\s*\??$",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        intent = _calc_builder(m, text)
        if intent is not None:
            intent.raw = text
        return intent

    rules.append(IntentRule(name="calculate", matcher=_calc_matcher))

    # ----- app launcher --------------------------------------------------
    rules.append(_make_regex_rule(
        "launch_app",
        r"\b(?:launch|start|run|open)\b\s+(?:the\s+)?(?:app(?:lication)?\s+)?"
        r"([A-Za-z][\w +.\-]{1,40}?)"
        r"(?:\s+(?:app(?:lication)?))?\s*$",
        lambda m, t: Intent("launch_app", {"name": m.group(1).strip()}),
    ))

    return rules


# ---------------------------------------------------------------------------
# CommandParser
# ---------------------------------------------------------------------------
_WAKE_PREFIX_RE = re.compile(
    r"^\s*(?:hey|hi|ok|okay|yo|hello)?\s*jarvis\s*[,.!?:;\-]*\s*",
    re.IGNORECASE,
)
# Trailing "jarvis" — people naturally say "thanks jarvis", "good night
# jarvis", etc. We strip it so the rule list doesn't have to repeat
# "(?:\s+jarvis)?" on every chat intent.
_WAKE_SUFFIX_RE = re.compile(
    r"\s*[,.!?:;\-]*\s*(?:hey\s+|hi\s+|ok\s+|okay\s+|yo\s+|hello\s+)?jarvis\s*[,.!?:;\-]*\s*$",
    re.IGNORECASE,
)


def strip_wake_prefix(text: str) -> str:
    """Strip a leading *or* trailing ``jarvis`` mention from ``text``.

    Handles all the obvious natural-language variants:
        "Hey JARVIS, what time is it"  -> "what time is it"
        "what time is it, JARVIS"      -> "what time is it"
        "JARVIS open chrome"           -> "open chrome"
        "thanks jarvis"                -> "thanks"
    """
    if not text:
        return ""
    cleaned = _WAKE_PREFIX_RE.sub("", text, count=1)
    cleaned = _WAKE_SUFFIX_RE.sub("", cleaned, count=1)
    return cleaned.strip()


class CommandParser:
    """Run text through the rule list and return the best matching intent."""

    def __init__(self) -> None:
        self._rules: List[IntentRule] = _build_rules()

    def parse(self, text: str) -> Intent:
        # Strip "Hey JARVIS, ..." / "JARVIS ..." so the rules don't have to
        # match the wake word every single time.
        cleaned = _strip(strip_wake_prefix(text))
        if not cleaned:
            return Intent("unknown", {"reason": "empty"}, raw=text or "")

        for rule in self._rules:
            intent = rule(cleaned)
            if intent is not None:
                intent.raw = text
                return intent

        return Intent("unknown", {"reason": "no_match"}, raw=text)
