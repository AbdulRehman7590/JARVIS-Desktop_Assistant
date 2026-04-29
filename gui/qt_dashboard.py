"""Modern PySide6 dashboard for JARVIS.

Why we left tkinter behind:
    * ``QSplitter`` gives the user a real, drag-resizable handle between
      the history sidebar and the conversation pane (the old Tk
      ``PanedWindow`` sash never grew/shrunk reliably and the columns
      couldn't be re-balanced).
    * Hiding the sidebar via the toggle just calls ``setVisible(False)``;
      Qt automatically re-flows the splitter so the conversation pane
      reclaims 100% of the width — fixing the long-standing "the toggle
      doesn't actually expand the conversation" complaint.
    * Native ``QSystemTrayIcon`` replaces the pystray dependency.
    * Speech-thread → UI marshalling uses Qt signals which are inherently
      thread-safe, removing the ``root.after(0, …)`` ceremony.

Voice flow:

    [wake-only mode]   listen for "hey jarvis ..." or just "jarvis ..."
                       │
        ┌── command in same breath ──> dispatch ──┐
        │                                          │
        └── wake only ──> "Yes?" ──> next phrase ──┤
                                                   ▼
                       [follow-up window: 14 s]

    [barge-in monitor]
        Whenever JARVIS is speaking we capture short 1.6 s windows
        and check for the wake word in the transcript. The moment the
        user says "Jarvis" over JARVIS, ``Speaker.interrupt()`` purges
        the rest of the reply and we drop straight into a follow-up
        listen — no second wake required.

Background mode:
    Hiding the dashboard (X-to-tray, "hide UI" voice command, or
    Ctrl+H) keeps JARVIS running in the system tray. A floating
    overlay near the top-right corner shows the current status and the
    latest reply so the user knows when JARVIS heard them.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, Callable, List, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.history import (
    History,
    HistoryEntry,
    RETENTION_DAYS,
    entry_local_dt,
    format_local,
)
from core.permissions import PermissionCategory
from gui.qt_settings import SettingsDialog
from gui.qt_theme import (
    Chip,
    IconButton,
    Palette,
    StatusDot,
    Toast,
    make_tray_icon,
    stylesheet,
)
from utils.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from main import JarvisApp

_log = get_logger()

# Seconds we keep listening without requiring "hey jarvis" again after a
# command was processed. The user explicitly asked for "a little longer"
# follow-up — 14 s is long enough for "...and the date?" after a sip of
# coffee but short enough that random room conversation doesn't get
# interpreted as a command.
_FOLLOW_UP_WINDOW_SEC = 14.0

# Short capture window we use while JARVIS is speaking, so barge-in feels
# instant — the listener returns within ~1.6 s of hearing "Jarvis".
_BARGE_IN_PHRASE_SEC = 1.6
_BARGE_IN_MIC_TIMEOUT_SEC = 1.5


# ---------------------------------------------------------------------------
# Bridge — lets background threads emit signals onto the UI thread.
# ---------------------------------------------------------------------------
class _Bridge(QObject):
    command_received = Signal(str)
    status_changed = Signal(str, str)          # state, text
    new_history_entry = Signal(object)         # HistoryEntry
    user_typed = Signal(str)
    jarvis_replied = Signal(str, str, bool)    # text, kind, success
    show_thinking = Signal(bool)
    show_toast = Signal(str, int, str)         # text, ms, colour
    permission_request = Signal(object, object)  # category, response_q
    confirm_request = Signal(str, object)       # question, response_q
    error_message = Signal(str)
    request_close = Signal()
    follow_up_set = Signal(float)               # absolute monotonic deadline


# ---------------------------------------------------------------------------
# Floating overlay (background mode + minimised state)
# ---------------------------------------------------------------------------
class FloatingOverlay(QWidget):
    """Frameless top-right widget shown while the main window is hidden."""

    REVEAL_MS = 7000

    clicked_to_restore = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowOpacity(0.95)

        outer = QFrame(self)
        outer.setStyleSheet(
            f"background:{Palette.ACCENT}; border-radius:12px;"
        )
        outer_l = QVBoxLayout(self)
        outer_l.setContentsMargins(0, 0, 0, 0)
        outer_l.addWidget(outer)

        inner = QFrame(outer)
        inner.setStyleSheet(
            f"background:{Palette.PANEL}; border-radius:10px;"
        )
        wrap = QVBoxLayout(outer)
        wrap.setContentsMargins(2, 2, 2, 2)
        wrap.addWidget(inner)

        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        head = QHBoxLayout()
        self.dot = StatusDot(self)
        head.addWidget(self.dot)
        brand = QLabel("JARVIS")
        brand.setObjectName("Brand")
        brand.setStyleSheet(f"color:{Palette.ACCENT}; font-weight:800;")
        head.addWidget(brand)
        head.addStretch(1)
        hint = QLabel("\u2197")
        hint.setStyleSheet(f"color:{Palette.FG_DIM}; font-weight:700;")
        head.addWidget(hint)
        v.addLayout(head)

        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet(
            f"color:{Palette.FG_DIM}; background:transparent;"
        )
        v.addWidget(self.status_label)

        self.reply_label = QLabel("")
        self.reply_label.setWordWrap(True)
        self.reply_label.setMaximumWidth(360)
        self.reply_label.setStyleSheet(
            f"color:{Palette.FG}; background:transparent;"
        )
        v.addWidget(self.reply_label)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self.collapse)

        self._expanded = False
        self.collapse(reposition=False)

    def expand(self, persist_ms: Optional[int] = None) -> None:
        self.status_label.show()
        self.reply_label.show()
        self._expanded = True
        self.adjustSize()
        self._anchor_top_right(min_width=360)
        if persist_ms:
            self._reveal_timer.start(persist_ms)

    def collapse(self, reposition: bool = True) -> None:
        self.reply_label.hide()
        self.status_label.hide()
        self._expanded = False
        if reposition:
            self.adjustSize()
            self._anchor_top_right(min_width=140)

    def _anchor_top_right(self, *, min_width: int) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        w = max(self.width(), min_width)
        x = geo.right() - w - 24
        y = geo.top() + 24
        self.setGeometry(x, y, w, max(self.height(), 56))

    def mousePressEvent(self, _e) -> None:  # noqa: N802
        self.clicked_to_restore.emit()


# ---------------------------------------------------------------------------
# Voice + button confirmation dialog
# ---------------------------------------------------------------------------
class VoiceConfirmDialog(QDialog):
    """Confirmation popup that accepts BOTH a button click and a voice reply.

    Why a custom dialog (instead of ``QMessageBox.question``):
        ``QMessageBox`` blocks the UI thread inside a native event loop
        we can't reach from the mic worker, so spoken "yes / no"
        couldn't dismiss it. This dialog exposes :meth:`accept` /
        :meth:`reject` slots that the dashboard's listen loop can fire
        from a background thread (via ``QTimer.singleShot(0, ...)``)
        the moment the classifier returns a verdict, while the buttons
        keep working exactly as the user expects.

    A small "voice indicator" footer shows the most recent transcript
    so the user can see whether JARVIS heard them correctly.
    """

    def __init__(self, question: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        heading = QLabel("\U0001F914  Just to confirm")
        heading.setStyleSheet(
            f"color:{Palette.ACCENT}; font-weight:800; font-size:13pt;"
        )
        layout.addWidget(heading)

        self._question_label = QLabel(question)
        self._question_label.setWordWrap(True)
        self._question_label.setStyleSheet(
            f"color:{Palette.FG}; font-size:11pt;"
        )
        layout.addWidget(self._question_label)

        hint = QLabel(
            "Click a button below \u2014 or just say "
            "\u201cyes\u201d / \u201cno\u201d aloud."
        )
        hint.setStyleSheet(
            f"color:{Palette.FG_DIM}; font-size:10pt; font-style:italic;"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._voice_label = QLabel("\U0001F3A4  Listening for your reply\u2026")
        self._voice_label.setStyleSheet(
            f"color:{Palette.ACCENT}; font-size:10pt; "
            "padding:8px 10px; "
            f"background:{Palette.PANEL_HI}; border-radius:8px;"
        )
        self._voice_label.setWordWrap(True)
        layout.addWidget(self._voice_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)

        self._no_btn = QPushButton("No, cancel")
        self._no_btn.setObjectName("Danger")
        self._no_btn.setMinimumHeight(38)
        self._no_btn.clicked.connect(self.reject)
        button_row.addWidget(self._no_btn)

        self._yes_btn = QPushButton("Yes, do it")
        self._yes_btn.setObjectName("Accent")
        self._yes_btn.setMinimumHeight(38)
        self._yes_btn.clicked.connect(self.accept)
        button_row.addWidget(self._yes_btn)

        layout.addLayout(button_row)

        # Default to the safer "No" button on Enter so a stray keystroke
        # never confirms a destructive op accidentally.
        self._no_btn.setDefault(True)
        self._no_btn.setAutoDefault(True)

    def show_voice_feedback(
        self, transcript: str, verdict: Optional[bool]
    ) -> None:
        """Update the live indicator with what we just heard."""
        if verdict is True:
            icon = "\u2705"
            colour = Palette.SUCCESS
            msg = f"Heard \u201c{transcript}\u201d \u2014 confirming."
        elif verdict is False:
            icon = "\u26D4"
            colour = Palette.ERROR
            msg = f"Heard \u201c{transcript}\u201d \u2014 cancelling."
        else:
            icon = "\U0001F3A4"
            colour = Palette.WARNING
            msg = (
                f"Heard \u201c{transcript}\u201d \u2014 "
                "I need a clearer yes or no."
            )
        self._voice_label.setText(f"{icon}  {msg}")
        self._voice_label.setStyleSheet(
            f"color:{colour}; font-size:10pt; padding:8px 10px; "
            f"background:{Palette.PANEL_HI}; border-radius:8px;"
        )


# ---------------------------------------------------------------------------
# History viewer — day-by-day timeline of the last 30 days
# ---------------------------------------------------------------------------
class HistoryDialog(QDialog):
    """Modal viewer that groups every kept chat by the day it happened on.

    The :class:`History` store enforces a hard 30-day retention, so this
    dialog never shows anything older. It's organised as a tree:

    * Top-level rows are local dates ("Today \u2013 Apr 29, 2026 (12)").
    * Children are the individual commands for that day, in
      chronological order, with the time, intent kind, and a snippet
      of the user's request and JARVIS's reply.

    A live filter at the top narrows by free-text (matches against the
    user's text, the response, or the intent kind) and a kind dropdown
    isolates a single command type. A details pane on the right shows
    the full text of whatever entry is selected, which is useful for
    long replies that get truncated in the tree.
    """

    def __init__(self, history: History, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            f"JARVIS \u00B7 Chat history (last {RETENTION_DAYS} days)"
        )
        self.setMinimumSize(760, 540)
        self._history = history

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        # ---- Header ----
        head = QLabel(
            f"\U0001F5C2  Showing your activity from the last "
            f"{RETENTION_DAYS} days. Older entries are automatically "
            f"forgotten."
        )
        head.setStyleSheet(
            f"color:{Palette.FG_DIM}; font-size:10pt; font-style:italic;"
        )
        head.setWordWrap(True)
        outer.addWidget(head)

        # ---- Filter row ----
        filter_row = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(
            "Search by text, intent, or reply\u2026"
        )
        self._filter.textChanged.connect(self._reload)
        filter_row.addWidget(self._filter, 2)

        self._kind_combo = QComboBox()
        self._kind_combo.addItem("All actions", "")
        self._kind_combo.currentIndexChanged.connect(self._reload)
        filter_row.addWidget(self._kind_combo, 1)

        outer.addLayout(filter_row)

        # ---- Body: tree on the left, details on the right ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(6)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["When", "Action", "Detail"])
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setColumnWidth(0, 130)
        self._tree.setColumnWidth(1, 130)
        self._tree.itemSelectionChanged.connect(self._on_select)
        splitter.addWidget(self._tree)

        details_panel = QFrame()
        details_panel.setObjectName("Card")
        d_layout = QVBoxLayout(details_panel)
        d_layout.setContentsMargins(12, 10, 12, 10)
        d_layout.setSpacing(6)
        details_head = QLabel("DETAILS")
        details_head.setObjectName("Subtle")
        d_layout.addWidget(details_head)
        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setPlaceholderText(
            "Select an entry on the left to see the full text."
        )
        d_layout.addWidget(self._details, 1)
        splitter.addWidget(details_panel)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 280])
        outer.addWidget(splitter, 1)

        # ---- Footer ----
        footer_row = QHBoxLayout()
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{Palette.FG_DIM}; font-size:10pt;")
        footer_row.addWidget(self._summary, 1)

        purge_btn = QPushButton("Purge old now")
        purge_btn.setToolTip(
            "Force-remove anything past the 30-day window right now."
        )
        purge_btn.clicked.connect(self._on_purge)
        footer_row.addWidget(purge_btn)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("Accent")
        close_btn.clicked.connect(self.accept)
        footer_row.addWidget(close_btn)
        outer.addLayout(footer_row)

        self._populate_kind_combo()
        self._reload()

    # ------------------------------------------------------------------
    def _populate_kind_combo(self) -> None:
        kinds = sorted({e.intent_kind for e in self._history.all()
                        if e.intent_kind})
        for k in kinds:
            self._kind_combo.addItem(k, k)

    def _reload(self) -> None:
        self._tree.clear()
        keyword = self._filter.text().strip() or None
        kind = self._kind_combo.currentData() or None

        groups = self._history.by_day(days=RETENTION_DAYS)
        total_entries = 0
        kept_days = 0

        from datetime import date as _date  # noqa: PLC0415
        today = _date.today()

        for day, entries in groups.items():
            # Apply filters per-day so empty days drop out cleanly.
            shown = [
                e for e in entries
                if (kind is None or e.intent_kind == kind)
                and (keyword is None
                     or keyword.lower() in e.user_text.lower()
                     or keyword.lower() in e.response.lower()
                     or keyword.lower() in e.intent_kind.lower())
            ]
            if not shown:
                continue
            kept_days += 1
            total_entries += len(shown)

            day_label = self._format_day_header(day, today, len(shown))
            day_node = QTreeWidgetItem([day_label, "", ""])
            day_font = day_node.font(0)
            day_font.setBold(True)
            day_node.setFont(0, day_font)
            day_node.setForeground(0, QColor(Palette.ACCENT))
            day_node.setFirstColumnSpanned(True)
            self._tree.addTopLevelItem(day_node)

            for entry in shown:
                local = entry_local_dt(entry)
                when = local.strftime("%I:%M %p").lstrip("0") if local else "?"
                snippet = entry.user_text or entry.response
                if len(snippet) > 80:
                    snippet = snippet[:77].rstrip() + "..."
                child = QTreeWidgetItem([when, entry.intent_kind, snippet])
                child.setForeground(
                    1,
                    QColor(Palette.SUCCESS if entry.success else Palette.ERROR),
                )
                child.setData(0, Qt.ItemDataRole.UserRole, entry)
                day_node.addChild(child)

            day_node.setExpanded(True)

        self._summary.setText(
            f"{total_entries} command{'s' if total_entries != 1 else ''} "
            f"across {kept_days} day{'s' if kept_days != 1 else ''} "
            f"(retention: {RETENTION_DAYS} days)."
        )
        if total_entries == 0:
            self._details.setPlainText(
                "No entries match the current filters."
            )

    @staticmethod
    def _format_day_header(day, today, count: int) -> str:
        delta = (today - day).days
        if delta == 0:
            label = "Today"
        elif delta == 1:
            label = "Yesterday"
        elif delta < 7:
            label = day.strftime("%A")  # Monday, Tuesday, ...
        else:
            label = day.strftime("%b %d")
        return (
            f"{label} \u2014 {day.strftime('%Y-%m-%d')} "
            f"({count} command{'s' if count != 1 else ''})"
        )

    def _on_select(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, HistoryEntry):
            self._details.clear()
            return
        when = format_local(entry.timestamp)
        status = "succeeded" if entry.success else "FAILED"
        self._details.setPlainText(
            f"{when}  \u00B7  {entry.intent_kind}  \u00B7  {status}\n\n"
            f"YOU: {entry.user_text or '(empty)'}\n\n"
            f"JARVIS: {entry.response or '(no reply)'}"
        )

    def _on_purge(self) -> None:
        removed = self._history.purge_old()
        self._populate_kind_combo()
        self._reload()
        self._summary.setText(
            self._summary.text() + f"  \u2022  Purged {removed} old "
            f"entr{'ies' if removed != 1 else 'y'}."
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class Dashboard(QMainWindow):
    """The polished PySide6 dashboard."""

    def __init__(
        self,
        app: "JarvisApp",
        voice_enabled: bool = True,
        wake_word: bool = True,
        start_hidden: bool = False,
    ) -> None:
        super().__init__()
        self.app = app
        self.voice_enabled = voice_enabled
        self.wake_word = wake_word
        self.start_hidden = start_hidden

        self._listener = None
        self._listen_thread: Optional[threading.Thread] = None
        self._stop_listening = threading.Event()
        self._command_queue: "queue.Queue[str]" = queue.Queue()
        self._busy = threading.Lock()

        self._follow_up_until: float = 0.0
        self._is_background = False
        self._closing = False
        self._last_reply_text = "Ready when you are."

        # While a confirmation dialog is showing this points at a
        # callable that consumes the next mic utterance instead of
        # treating it as a new command. ``None`` ⇒ normal command flow.
        self._voice_capture_target: Optional[Callable[[str], None]] = None

        self.bridge = _Bridge()
        self._wire_bridge()

        self._build_ui()
        self._build_overlay()
        self._build_tray()

        self._wire_app_to_ui()

        if self.voice_enabled:
            self._init_listener()

        if self.start_hidden:
            QTimer.singleShot(200, self._hide_to_background)

    # ==================================================================
    # Bridge wiring
    # ==================================================================
    def _wire_bridge(self) -> None:
        b = self.bridge
        b.command_received.connect(self._on_command_received)
        b.status_changed.connect(self._apply_status)
        b.new_history_entry.connect(lambda _e: self._refresh_history_view())
        b.jarvis_replied.connect(self._post_jarvis)
        b.show_thinking.connect(self._toggle_thinking)
        b.show_toast.connect(self._show_toast)
        b.permission_request.connect(self._open_permission_dialog)
        b.confirm_request.connect(self._open_confirm_dialog)
        b.error_message.connect(lambda t: self._post_error(t))
        b.request_close.connect(self.close)
        b.follow_up_set.connect(self._set_follow_up_until)

    def _set_follow_up_until(self, deadline: float) -> None:
        self._follow_up_until = deadline

    # ==================================================================
    # UI construction
    # ==================================================================
    def _build_ui(self) -> None:
        self.setWindowTitle("JARVIS")
        self.setWindowIcon(make_tray_icon(64))
        self.resize(1240, 760)
        self.setMinimumSize(720, 520)
        self.setStyleSheet(stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        v.addWidget(self._build_titlebar())
        # Body with padding
        body_wrap = QWidget()
        body_l = QHBoxLayout(body_wrap)
        body_l.setContentsMargins(14, 12, 14, 12)
        body_l.addWidget(self._build_body(), 1)
        v.addWidget(body_wrap, 1)

        # Toast
        self.toast = Toast(central)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+,"), self, self._open_settings)
        QShortcut(QKeySequence("Ctrl+H"), self, self._hide_to_background)

        self.entry.setFocus()

    # ------------------------------------------------------------------
    def _build_titlebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setMinimumHeight(64)
        bar.setMaximumHeight(64)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)

        self.sidebar_toggle = IconButton(
            "\u2630", palette="muted", size=36,
            tooltip="Toggle history (Ctrl+H hides the whole window)",
        )
        self.sidebar_toggle.clicked.connect(self._toggle_sidebar)
        h.addWidget(self.sidebar_toggle)

        self.status_dot = StatusDot()
        h.addWidget(self.status_dot)

        brand = QLabel("JARVIS")
        brand.setObjectName("Brand")
        h.addWidget(brand)

        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("StatusText")
        h.addWidget(self.status_label)

        self.thinking_bar = QProgressBar()
        self.thinking_bar.setRange(0, 0)
        self.thinking_bar.setFixedWidth(180)
        self.thinking_bar.setFixedHeight(6)
        self.thinking_bar.setTextVisible(False)
        self.thinking_bar.hide()
        h.addWidget(self.thinking_bar)

        h.addStretch(1)

        self.user_pill = self._make_pill("\U0001F464 Guest")
        h.addWidget(self.user_pill)
        self.backend_pill = self._make_pill("Voice off")
        h.addWidget(self.backend_pill)
        self.llm_pill = self._make_pill("\U0001F9E0 LLM off")
        h.addWidget(self.llm_pill)

        settings_btn = QPushButton("\u2699  Settings")
        settings_btn.clicked.connect(self._open_settings)
        h.addWidget(settings_btn)

        quit_btn = QPushButton("\u2715  Quit")
        quit_btn.setObjectName("Danger")
        quit_btn.clicked.connect(self._on_close)
        h.addWidget(quit_btn)

        return bar

    @staticmethod
    def _make_pill(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("Pill")
        return lbl

    # ------------------------------------------------------------------
    def _build_body(self) -> QSplitter:
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        sidebar = self._build_sidebar()
        conversation = self._build_conversation()
        self._apply_panel_shadow(sidebar)
        self._apply_panel_shadow(conversation)
        self.splitter.addWidget(sidebar)
        self.splitter.addWidget(conversation)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 920])
        return self.splitter

    @staticmethod
    def _apply_panel_shadow(widget: QWidget) -> None:
        """Add a subtle drop shadow so panels feel raised."""
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 110))
        widget.setGraphicsEffect(shadow)

    # ------------------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Panel")
        side.setMinimumWidth(220)
        v = QVBoxLayout(side)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        head = QLabel("HISTORY")
        head.setObjectName("Heading")
        v.addWidget(head)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter\u2026")
        self.filter_edit.textChanged.connect(self._refresh_history_view)
        v.addWidget(self.filter_edit)

        self.history_list = QListWidget()
        self.history_list.setAlternatingRowColors(False)
        self.history_list.itemSelectionChanged.connect(
            self._on_history_select)
        v.addWidget(self.history_list, 1)

        view_btn = QPushButton("\U0001F5C2  View by day\u2026")
        view_btn.setToolTip(
            f"Open the day-by-day history viewer "
            f"(last {RETENTION_DAYS} days)."
        )
        view_btn.clicked.connect(self._open_history_dialog)
        v.addWidget(view_btn)

        clear_btn = QPushButton("Clear history")
        clear_btn.clicked.connect(self._clear_history)
        v.addWidget(clear_btn)

        self._sidebar_widget = side
        return side

    def _open_history_dialog(self) -> None:
        """Show the day-wise history viewer dialog."""
        dlg = HistoryDialog(self.app.history, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------
    def _build_conversation(self) -> QWidget:
        right = QFrame()
        right.setObjectName("Panel")
        v = QVBoxLayout(right)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("CONVERSATION")
        title.setObjectName("Heading")
        head.addWidget(title)
        head.addStretch(1)
        v.addLayout(head)

        # Transcript
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFrameStyle(QFrame.Shape.NoFrame)
        # Opacity effect powers the gentle fade-in animation that runs
        # whenever a new reply lands in the transcript.
        self._transcript_opacity = QGraphicsOpacityEffect(self.transcript)
        self._transcript_opacity.setOpacity(1.0)
        self.transcript.setGraphicsEffect(self._transcript_opacity)
        self._transcript_anim = QPropertyAnimation(
            self._transcript_opacity, b"opacity", self
        )
        self._transcript_anim.setDuration(280)
        self._transcript_anim.setStartValue(0.55)
        self._transcript_anim.setEndValue(1.0)
        self._transcript_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        v.addWidget(self.transcript, 1)

        # Quick chips
        chip_head = QLabel("QUICK ACTIONS")
        chip_head.setObjectName("Subtle")
        v.addWidget(chip_head)

        chip_container = QWidget()
        self._chip_grid = QGridLayout(chip_container)
        self._chip_grid.setContentsMargins(0, 0, 0, 0)
        self._chip_grid.setHorizontalSpacing(6)
        self._chip_grid.setVerticalSpacing(6)

        self._chip_specs: List[tuple[str, str]] = [
            ("\u23F0 Time",        "what time is it"),
            ("\U0001F4C5 Date",    "what is the date"),
            ("\U0001F604 Joke",    "tell me a joke"),
            ("\U0001F5BC Screenshot", "take a screenshot"),
            ("\U0001F4CA System info", "system info"),
            ("\U0001F50A Volume up",   "volume up"),
            ("\U0001F507 Mute",        "mute"),
            ("\U0001F441 Hide UI",     "hide ui"),
            ("\u2753 Help",        "help"),
        ]
        self._chips: List[Chip] = []
        for label, cmd in self._chip_specs:
            chip = Chip(label)
            chip.clicked.connect(
                lambda _checked=False, c=cmd: self._submit_text(c)
            )
            self._chips.append(chip)
        self._chip_container = chip_container
        v.addWidget(chip_container)
        self._reflow_chips()

        # Entry row
        entry_row = QHBoxLayout()
        entry_row.setSpacing(10)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Type a command\u2026")
        font = QFont(self.entry.font())
        font.setPointSize(13)
        self.entry.setFont(font)
        self.entry.returnPressed.connect(self._submit_typed)
        self.entry.installEventFilter(self)
        entry_row.addWidget(self.entry, 1)

        self.mic_btn = IconButton(
            "\U0001F3A4", palette="muted", size=46,
            tooltip="Toggle microphone",
        )
        self.mic_btn.clicked.connect(self._toggle_mic)
        entry_row.addWidget(self.mic_btn)

        self.send_btn = IconButton(
            "\u27A4", palette="accent", size=46,
            tooltip="Send (Enter)",
        )
        self.send_btn.clicked.connect(self._submit_typed)
        entry_row.addWidget(self.send_btn)

        v.addLayout(entry_row)

        self._history_index = 0
        return right

    # ------------------------------------------------------------------
    # Chip layout reflow on resize
    # ------------------------------------------------------------------
    _CHIP_AVG_WIDTH = 150

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._reflow_chips()

    def _reflow_chips(self) -> None:
        if not hasattr(self, "_chips") or not self._chips:
            return
        width = max(self._chip_container.width(), self.width() // 2)
        cols = max(2, width // self._CHIP_AVG_WIDTH)
        cols = min(cols, len(self._chips))
        # Clear grid
        while self._chip_grid.count():
            item = self._chip_grid.takeAt(0)
            if item is not None:
                w = item.widget()
                if w is not None:
                    w.setParent(None)  # detach but keep alive
        for i, chip in enumerate(self._chips):
            r, c = divmod(i, cols)
            self._chip_grid.addWidget(chip, r, c)
            chip.setParent(self._chip_container)
            chip.show()
        for c in range(cols):
            self._chip_grid.setColumnStretch(c, 1)

    # ------------------------------------------------------------------
    # Up/Down arrow history navigation in entry
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.entry and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Up:
                self._history_prev()
                return True
            if event.key() == Qt.Key.Key_Down:
                self._history_next()
                return True
        return super().eventFilter(obj, event)

    def _history_prev(self) -> None:
        entries = self.app.history.latest(50)
        msgs = [e.user_text for e in entries if e.user_text]
        if not msgs:
            return
        self._history_index = min(self._history_index + 1, len(msgs))
        self.entry.setText(msgs[-self._history_index])
        self.entry.setCursorPosition(len(self.entry.text()))

    def _history_next(self) -> None:
        entries = self.app.history.latest(50)
        msgs = [e.user_text for e in entries if e.user_text]
        if not msgs or self._history_index <= 1:
            self._history_index = 0
            self.entry.clear()
            return
        self._history_index -= 1
        self.entry.setText(msgs[-self._history_index])
        self.entry.setCursorPosition(len(self.entry.text()))

    # ==================================================================
    # Sidebar toggle
    # ==================================================================
    def _toggle_sidebar(self) -> None:
        visible = not self._sidebar_widget.isVisible()
        self._sidebar_widget.setVisible(visible)
        # When hidden the splitter immediately gives all width to the
        # remaining (conversation) pane. When shown, restore a sensible
        # default split.
        if visible:
            total = self.splitter.width() or self.width()
            self.splitter.setSizes([min(360, max(220, total // 4)),
                                    max(420, total - 360)])
            self.sidebar_toggle.set_glyph("\u2630")
        else:
            self.sidebar_toggle.set_glyph("\u25b8")

    # ==================================================================
    # Tray
    # ==================================================================
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_tray_icon(64), self)
        self.tray.setToolTip("JARVIS")

        menu = QMenu()
        show_act = QAction("Show JARVIS", self)
        show_act.triggered.connect(self._restore_window)
        hide_act = QAction("Hide JARVIS", self)
        hide_act.triggered.connect(self._hide_to_background)
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._on_close)
        menu.addAction(show_act)
        menu.addAction(hide_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible():
                self._hide_to_background()
            else:
                self._restore_window()

    # ==================================================================
    # Overlay
    # ==================================================================
    def _build_overlay(self) -> None:
        self.overlay = FloatingOverlay()
        self.overlay.clicked_to_restore.connect(self._restore_window)

    def _show_overlay(self, expanded: bool = False) -> None:
        if expanded:
            self.overlay.expand(persist_ms=FloatingOverlay.REVEAL_MS)
        else:
            self.overlay.collapse()
        self.overlay.show()
        self.overlay.raise_()

    def _hide_overlay(self) -> None:
        self.overlay.hide()

    def _reveal_overlay_with(self, text: str) -> None:
        if not text:
            return
        snippet = text if len(text) <= 240 else text[:237].rstrip() + "\u2026"
        self.overlay.reply_label.setText(snippet)
        self.overlay.expand(persist_ms=FloatingOverlay.REVEAL_MS)

    # ==================================================================
    # Wiring app → UI
    # ==================================================================
    def _wire_app_to_ui(self) -> None:
        # Permission + confirm prompts go through the UI thread.
        self.app._permission_prompter = self._ask_permission_threadsafe
        self.app._confirmer = self._confirm_threadsafe
        self.app.permissions._prompter = self._ask_permission_threadsafe   # type: ignore[attr-defined]
        self.app.files._confirm = self._confirm_threadsafe                  # type: ignore[attr-defined]
        self.app.system._confirm = self._confirm_threadsafe                 # type: ignore[attr-defined]

        # Voice-controllable window hooks.
        self.app.executor.on_show_gui = self._show_gui_from_voice
        self.app.executor.on_hide_gui = self._hide_gui_from_voice

        self._refresh_user_pill()
        self._refresh_llm_pill()

        self.app.history.subscribe(
            lambda e: self.bridge.new_history_entry.emit(e)
        )
        self._refresh_history_view()

        # Live-update the user pill the moment ``memory['user_name']``
        # changes (e.g. user runs "call me Tony" or edits settings).
        try:
            self.app.memory.subscribe(self._on_memory_changed)
        except AttributeError:
            # Older memory module without observer support — no-op.
            pass

        if not self.app.memory.has_user_name():
            greeting = "Hello. I am JARVIS. What is your name?"
            self._post_jarvis(greeting, "jarvis_msg", True)
            self._speak_async(
                "Hello. I am JARVIS. Please tell me your name in the box below."
            )
        else:
            greeting = f"Welcome back, {self.app.memory.user_name}."
            self._post_jarvis(greeting, "jarvis_msg", True)
            self._speak_async(greeting)

    # ==================================================================
    # Speech (spoken in worker so the UI stays smooth)
    # ==================================================================
    def _speak_async(self, text: str) -> None:
        """Speak ``text`` from a worker thread so the UI never blocks.

        Always use this for GUI-initiated speech. Voice-only flows that
        need to listen right after speaking should call
        ``app.speaker.speak(text, wait=True)`` directly.
        """
        if not text:
            return
        threading.Thread(
            target=lambda: self.app.speaker.speak(text, wait=False),
            daemon=True,
            name="jarvis-speak",
        ).start()

    # ==================================================================
    # Microphone listening loop (unchanged behaviour, threadsafe via signals)
    # ==================================================================
    def _init_listener(self) -> None:
        try:
            from speech.speech_to_text import Listener  # noqa: PLC0415

            self._listener = Listener()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Listener init failed: %s", exc)
            self._set_mic_state(available=False)
            self.voice_enabled = False
            return

        if not self._listener.has_microphone:
            self._set_mic_state(available=False)
            self.voice_enabled = False
            return

        self.backend_pill.setText(f"\U0001F3A4 {self._listener.backend}")
        self._set_mic_state(available=True, on=True)
        self._stop_listening.clear()
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="jarvis-mic",
        )
        self._listen_thread.start()
        QTimer.singleShot(120, self._drain_queue)

    def _listen_loop(self) -> None:
        assert self._listener is not None
        from speech.speech_to_text import (  # noqa: PLC0415
            contains_wake_phrase,
            strip_wake_prefix,
        )

        while not self._stop_listening.is_set():
            speaker = self.app.speaker
            speaking = bool(getattr(speaker, "is_speaking", False))

            # ----- VOICE-CONFIRM ROUTING (highest priority) ---------
            # When a confirmation dialog is up we hijack the listener
            # entirely: every captured utterance is forwarded to the
            # dialog's handler instead of going through the normal
            # wake-word / command pipeline. We deliberately wait until
            # JARVIS finishes speaking so the mic doesn't pick up our
            # own TTS output as the user's reply.
            target = self._voice_capture_target
            if target is not None:
                if speaking:
                    time.sleep(0.1)
                    continue
                self.bridge.status_changed.emit(
                    "listening", "Listening for yes / no\u2026"
                )
                try:
                    text = self._listener.listen_once(
                        timeout=2.0, phrase_time_limit=3.0,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.debug("Confirm capture error: %s", exc)
                    text = None
                if text:
                    try:
                        target(text)
                    except Exception as exc:  # noqa: BLE001
                        _log.debug("Confirm route error: %s", exc)
                continue

            # ----- BARGE-IN MONITOR ----------------------------------
            # Highest priority: while JARVIS is talking, capture short
            # 1.6 s windows and check for the wake word. The instant we
            # hear "Jarvis" we cut the speech and drop straight into a
            # follow-up listen — no "Yes?" needed.
            if speaking:
                self.bridge.status_changed.emit(
                    "speaking", "Speaking\u2026 (say 'Jarvis' to interrupt)"
                )
                try:
                    text = self._listener.listen_once(
                        timeout=_BARGE_IN_MIC_TIMEOUT_SEC,
                        phrase_time_limit=_BARGE_IN_PHRASE_SEC,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.debug("Barge-in capture error: %s", exc)
                    text = None
                if text and contains_wake_phrase(text):
                    _log.info("Barge-in detected: %r", text)
                    try:
                        speaker.interrupt()
                    except Exception:  # noqa: BLE001
                        pass
                    self.bridge.show_toast.emit(
                        "Listening\u2026", 1600, Palette.ACCENT
                    )
                    stripped, _wake = strip_wake_prefix(text)
                    if stripped:
                        # User said "Jarvis, do X" — dispatch immediately.
                        self.bridge.follow_up_set.emit(
                            time.monotonic() + _FOLLOW_UP_WINDOW_SEC
                        )
                        self._command_queue.put(stripped)
                    else:
                        # Bare wake word — extend follow-up so the next
                        # phrase is captured without re-waking.
                        self.bridge.follow_up_set.emit(
                            time.monotonic() + _FOLLOW_UP_WINDOW_SEC
                        )
                continue

            in_follow_up = (
                self.wake_word
                and time.monotonic() < self._follow_up_until
            )

            if not self.wake_word:
                self.bridge.status_changed.emit("listening", "Listening\u2026")
                try:
                    text = self._listener.listen_once(timeout=6.0)
                except Exception as exc:  # noqa: BLE001
                    _log.error("Mic loop error: %s", exc)
                    text = None
                if text:
                    self._command_queue.put(text)
                self.bridge.status_changed.emit("idle", "Idle")
                continue

            if in_follow_up:
                remaining = max(0, int(self._follow_up_until - time.monotonic()))
                self.bridge.status_changed.emit(
                    "listening", f"Listening for follow-up ({remaining}s)\u2026"
                )
                try:
                    text = self._listener.listen_once(timeout=2.0)
                except Exception as exc:  # noqa: BLE001
                    _log.error("Mic follow-up error: %s", exc)
                    text = None
                if text:
                    stripped, _wake = strip_wake_prefix(text)
                    payload = stripped or text.strip()
                    if payload:
                        self.bridge.follow_up_set.emit(
                            time.monotonic() + _FOLLOW_UP_WINDOW_SEC
                        )
                        self._command_queue.put(payload)
                continue

            self.bridge.status_changed.emit(
                "listening", "Awaiting 'Jarvis'\u2026")
            try:
                kind, payload = self._listener.listen_for_wake_or_command(
                    timeout=8.0,
                )
            except Exception as exc:  # noqa: BLE001
                _log.error("Mic wake error: %s", exc)
                kind, payload = "silence", ""

            if kind == "command":
                if self._is_background:
                    QTimer.singleShot(
                        0, lambda: self._reveal_overlay_with("Listening\u2026"))
                self._command_queue.put(payload)
            elif kind == "wake_only":
                self.bridge.show_toast.emit(
                    "Yes? I'm listening\u2026", 2200, Palette.ACCENT)
                self.bridge.status_changed.emit(
                    "listening", "Yes? I'm listening\u2026")
                # Voice-only path: needs to wait so we don't capture our
                # own "Yes?" reply.
                self.app.speaker.speak("Yes?", wait=True)
                try:
                    follow = self._listener.listen_once(timeout=8.0)
                except Exception as exc:  # noqa: BLE001
                    _log.error("Mic post-wake error: %s", exc)
                    follow = None
                if follow:
                    stripped, _wake = strip_wake_prefix(follow)
                    if stripped:
                        self._command_queue.put(stripped)
            elif kind == "ignored":
                _log.debug("Ignored (no wake): %r", payload)

            self.bridge.status_changed.emit("idle", "Idle")

    def _drain_queue(self) -> None:
        try:
            while True:
                text = self._command_queue.get_nowait()
                self._post_user(text)
                self._dispatch_command(text)
        except queue.Empty:
            pass
        if not self._closing:
            QTimer.singleShot(120, self._drain_queue)

    def _set_mic_state(self, *, available: bool, on: bool = False) -> None:
        if not available:
            self.mic_btn.set_palette("muted")
            self.mic_btn.set_pulse(False)
            self.backend_pill.setText("\U0001F3A4 unavailable")
            return
        self.mic_btn.set_palette("accent" if on else "muted")
        self.mic_btn.set_pulse(on)

    def _toggle_mic(self) -> None:
        if not self.voice_enabled:
            self.toast.show_message(
                "Microphone unavailable on this system.",
                colour=Palette.WARNING,
            )
            return
        if self._listen_thread and self._listen_thread.is_alive():
            self._stop_listening.set()
            self._set_mic_state(available=True, on=False)
            self._apply_status("idle", "Mic paused")
            self.toast.show_message("Microphone paused.",
                                    colour=Palette.WARNING)
        else:
            self._init_listener()
            self.toast.show_message("Microphone resumed.",
                                    colour=Palette.SUCCESS)

    # ==================================================================
    # Command dispatch
    # ==================================================================
    def _on_command_received(self, text: str) -> None:
        self._post_user(text)
        self._dispatch_command(text)

    def _submit_typed(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        self._submit_text(text)

    def _submit_text(self, text: str) -> None:
        self._post_user(text)
        # First-run name capture
        if not self.app.memory.has_user_name():
            from main import clean_name_input  # local import to avoid cycle
            cleaned = clean_name_input(text)
            if cleaned and not text.lower().startswith(
                ("my name is", "i am", "i'm", "call me", "this is")
            ):
                self.app.memory.user_name = cleaned
                self._refresh_user_pill()
                msg = f"Pleased to meet you, {cleaned}."
                self._post_jarvis(msg, "jarvis_msg", True)
                self._speak_async(msg)
                return
        self._dispatch_command(text)

    def _dispatch_command(self, text: str) -> None:
        def _worker() -> None:
            with self._busy:
                self.bridge.status_changed.emit("thinking", "Thinking\u2026")
                self.bridge.show_thinking.emit(True)
                try:
                    result = self.app.executor.handle(text)
                except Exception as exc:  # noqa: BLE001
                    _log.exception("Dispatch crashed")
                    self.bridge.error_message.emit(f"Internal error: {exc}")
                    self.bridge.show_thinking.emit(False)
                    self.bridge.status_changed.emit("error", "Error")
                    return

                if self.app.memory.user_name:
                    QTimer.singleShot(0, self._refresh_user_pill)

                # Speak EVERY reply (the user explicitly wants this).
                # Use wait=False so the GUI doesn't block on TTS.
                if result.response:
                    threading.Thread(
                        target=lambda r=result.response:
                            self.app.speaker.speak(r, wait=False),
                        daemon=True,
                        name="jarvis-speak-reply",
                    ).start()

                self.bridge.status_changed.emit(
                    "speaking" if result.success else "error",
                    "Replying\u2026" if result.success else "Failed",
                )

                tag = ("chat_msg" if result.source == "chat"
                       else "jarvis_msg")
                self.bridge.jarvis_replied.emit(
                    result.response, tag, result.success
                )
                self.bridge.show_thinking.emit(False)

                if not result.success:
                    self.bridge.show_toast.emit(
                        result.response[:80], 2400, Palette.ERROR
                    )

                if self.wake_word and self.voice_enabled:
                    self.bridge.follow_up_set.emit(
                        time.monotonic() + _FOLLOW_UP_WINDOW_SEC
                    )

                QTimer.singleShot(
                    800, lambda: self._apply_status("idle", "Idle"))

                if result.should_exit:
                    QTimer.singleShot(500, self.close)

        threading.Thread(target=_worker, daemon=True,
                         name="jarvis-cmd").start()

    # ==================================================================
    # Modal prompts (thread-safe via signals)
    # ==================================================================
    def _ask_permission_threadsafe(self, category: PermissionCategory) -> str:
        response_q: "queue.Queue[str]" = queue.Queue()
        self.bridge.permission_request.emit(category, response_q)
        try:
            return response_q.get(timeout=120.0)
        except queue.Empty:
            return "no"

    def _confirm_threadsafe(self, question: str) -> bool:
        response_q: "queue.Queue[bool]" = queue.Queue()
        self.bridge.confirm_request.emit(question, response_q)
        try:
            return response_q.get(timeout=120.0)
        except queue.Empty:
            return False

    def _open_permission_dialog(self, category, response_q) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("Permission request")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("JARVIS needs permission")
        msg.setInformativeText(category.description)
        yes_btn = msg.addButton("Yes (this time)",
                                QMessageBox.ButtonRole.AcceptRole)
        always_btn = msg.addButton("Always",
                                   QMessageBox.ButtonRole.AcceptRole)
        no_btn = msg.addButton("No",
                               QMessageBox.ButtonRole.RejectRole)
        never_btn = msg.addButton("Never",
                                  QMessageBox.ButtonRole.DestructiveRole)
        msg.setDefaultButton(no_btn)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is yes_btn:
            response_q.put("yes")
        elif clicked is always_btn:
            response_q.put("always")
        elif clicked is never_btn:
            response_q.put("never")
        else:
            response_q.put("no")

    def _open_confirm_dialog(self, question: str, response_q) -> None:
        """Show a confirm popup that accepts both clicks AND spoken yes/no.

        The dashboard's mic loop is temporarily routed into the dialog
        for the duration of ``exec()``: while the popup is visible
        every utterance is classified by :func:`classify_yes_no` and,
        when conclusive, drives the dialog's accept/reject — the same
        actions the buttons trigger.
        """
        from speech.speech_to_text import (  # noqa: PLC0415
            classify_yes_no,
        )

        dlg = VoiceConfirmDialog(question, parent=self)

        # Speak the question if voice is on so the user actually
        # *hears* what they're confirming. We wait for TTS to finish
        # before listening (the route-target check at the top of
        # `_listen_loop` already does this), so spoken "Yes / No"
        # prompts don't get echoed back into the classifier.
        if self.voice_enabled:
            self._speak_async(question)

        def on_voice(text: str) -> None:
            verdict = classify_yes_no(text)
            # Bounce UI updates onto the Qt thread.
            QTimer.singleShot(
                0, lambda: dlg.show_voice_feedback(text, verdict)
            )
            if verdict is True:
                QTimer.singleShot(0, dlg.accept)
            elif verdict is False:
                QTimer.singleShot(0, dlg.reject)

        self._voice_capture_target = on_voice
        try:
            result = dlg.exec()
        finally:
            self._voice_capture_target = None

        accepted = result == QDialog.DialogCode.Accepted
        response_q.put(accepted)

    # ==================================================================
    # Status / animations
    # ==================================================================
    def _apply_status(self, indicator_state: str, text: str) -> None:
        self.status_dot.set_state(indicator_state)
        self.status_label.setText(text)
        self.overlay.dot.set_state(indicator_state)
        self.overlay.status_label.setText(text)

    def _toggle_thinking(self, on: bool) -> None:
        self.thinking_bar.setVisible(on)

    def _show_toast(self, text: str, ms: int, colour: str) -> None:
        self.toast.show_message(text, duration_ms=ms, colour=colour)

    # ==================================================================
    # Window state — show / hide / overlay
    # ==================================================================
    def _hide_to_background(self) -> None:
        """Fully hide the window (no taskbar entry).

        We've seen Windows occasionally treat ``self.hide()`` on a
        currently-minimised window as "restore + iconify" instead of
        "remove from screen", which leaves a taskbar button behind. To
        avoid that we:

        1. Clear any minimised window state so Qt doesn't carry it over
           on the next ``show()``.
        2. Hide the window itself.
        3. Hide it from the taskbar specifically (defensive — also
           guards against quirky Window managers that treat hidden
           top-levels as iconified).
        4. Make sure the tray icon is visible so the user has an obvious
           recovery handle.
        """
        self._is_background = True
        try:
            self.setWindowState(self.windowState()
                                & ~Qt.WindowState.WindowMinimized)
        except Exception:  # noqa: BLE001
            pass
        self.hide()
        # Process any pending events so the hide actually lands before
        # we check tray state below.
        QApplication.processEvents()
        try:
            if self.tray is not None and not self.tray.isVisible():
                self.tray.show()
        except Exception:  # noqa: BLE001
            pass
        self._show_overlay(expanded=False)
        try:
            self.tray.showMessage(
                "JARVIS",
                "Running in background. Say 'hey JARVIS' or click the "
                "tray icon to bring me back.",
                make_tray_icon(64),
                2400,
            )
        except Exception:  # noqa: BLE001
            pass
        self.toast.show_message(
            "Running in background. Say 'hey JARVIS' anytime.",
            duration_ms=2400, colour=Palette.ACCENT,
        )

    def _restore_window(self) -> None:
        self._is_background = False
        self._hide_overlay()
        self.show()
        self.raise_()
        self.activateWindow()
        if self.isMinimized():
            self.showNormal()

    def _show_gui_from_voice(self) -> bool:
        QTimer.singleShot(0, self._restore_window)
        return True

    def _hide_gui_from_voice(self) -> bool:
        QTimer.singleShot(0, self._hide_to_background)
        return True

    def changeEvent(self, e) -> None:  # noqa: N802
        if e.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._is_background = True
                self._show_overlay(expanded=False)
        super().changeEvent(e)

    # ==================================================================
    # Transcript helpers
    # ==================================================================
    def _format(self, color: str, *, bold: bool = False,
                size: int = 12, italic: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        font = QFont("Segoe UI", size)
        font.setBold(bold)
        font.setItalic(italic)
        fmt.setFont(font)
        return fmt

    def _post_user(self, text: str) -> None:
        if not text:
            return
        self._append_block("YOU", text, Palette.USER, Palette.FG)

    def _post_jarvis(self, text: str, kind: str, success: bool) -> None:
        if not text:
            return
        label = "JARVIS \u00B7 chat" if kind == "chat_msg" else "JARVIS"
        msg_color = Palette.CHAT if kind == "chat_msg" else Palette.FG
        self._append_block(label, text, Palette.JARVIS, msg_color,
                           italic=(kind == "chat_msg"))
        self._last_reply_text = text
        if self._is_background:
            self._reveal_overlay_with(text)

    def _post_error(self, text: str) -> None:
        if not text:
            return
        self._append_block("ERROR", text, Palette.ERROR, Palette.ERROR)

    def _append_block(self, label: str, text: str,
                      label_color: str, text_color: str,
                      *, italic: bool = False) -> None:
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        cursor.insertText(label, self._format(label_color,
                                              bold=True, size=9))
        cursor.insertBlock()
        cursor.insertText(text, self._format(text_color, size=12,
                                             italic=italic))
        cursor.insertBlock()
        self.transcript.setTextCursor(cursor)
        sb = self.transcript.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._play_transcript_fade()

    def _play_transcript_fade(self) -> None:
        """Brief fade-in so each new message draws the eye."""
        anim = getattr(self, "_transcript_anim", None)
        if anim is None:
            return
        try:
            anim.stop()
            anim.start()
        except RuntimeError:
            # Effect was deleted (window closing) — ignore.
            pass

    # ==================================================================
    # History wiring
    # ==================================================================
    def _refresh_history_view(self) -> None:
        needle = (self.filter_edit.text() or "").strip().lower()
        self.history_list.clear()

        # Group the (filtered) recent entries by day so the sidebar
        # shows clear "Today", "Yesterday", "Apr 25" dividers — same
        # structure the full HistoryDialog uses, just trimmed for the
        # narrow column.
        from datetime import date as _date  # noqa: PLC0415
        today = _date.today()

        groups = self.app.history.by_day(days=RETENTION_DAYS)
        for day, entries in groups.items():
            shown = [
                e for e in entries
                if not needle
                or needle in e.user_text.lower()
                or needle in e.intent_kind.lower()
                or needle in e.response.lower()
            ]
            if not shown:
                continue
            delta = (today - day).days
            if delta == 0:
                day_label = "TODAY"
            elif delta == 1:
                day_label = "YESTERDAY"
            elif delta < 7:
                day_label = day.strftime("%A").upper()
            else:
                day_label = day.strftime("%b %d").upper()
            divider_text = f"\u2500\u2500 {day_label} \u00B7 {len(shown)}"
            divider = QListWidgetItem(divider_text)
            divider.setFlags(Qt.ItemFlag.NoItemFlags)
            divider.setForeground(QColor(Palette.ACCENT))
            font = divider.font()
            font.setBold(True)
            font.setPointSize(max(font.pointSize() - 1, 8))
            divider.setFont(font)
            self.history_list.addItem(divider)

            for entry in reversed(shown):
                local = entry_local_dt(entry)
                when = (local.strftime("%I:%M %p").lstrip("0")
                        if local else "?")
                item_text = f"  {when}\n    {entry.intent_kind}"
                it = QListWidgetItem(item_text)
                it.setForeground(QColor(Palette.FG if entry.success
                                        else Palette.ERROR))
                it.setData(Qt.ItemDataRole.UserRole, entry)
                self.history_list.addItem(it)

    def _on_history_select(self) -> None:
        it = self.history_list.currentItem()
        if it is None:
            return
        entry: HistoryEntry = it.data(Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        snippet = (entry.response or "")[:80]
        self.toast.show_message(
            f"{entry.intent_kind} \u00B7 {snippet}",
            colour=Palette.ACCENT, duration_ms=2400,
        )

    # ==================================================================
    # Toolbar handlers
    # ==================================================================
    def _open_settings(self) -> None:
        dlg = SettingsDialog(
            self,
            app=self.app,
            on_llm_saved=self._on_llm_saved,
            on_permissions_changed=self._on_permissions_changed,
        )
        dlg.exec()

    def _on_llm_saved(self, configured: bool) -> None:
        from core.llm_client import LLMClient  # noqa: PLC0415

        self.app.llm = LLMClient()
        self.app.executor.llm = self.app.llm
        self._refresh_llm_pill()
        self.toast.show_message(
            "LLM connected." if configured else "LLM disabled.",
            colour=Palette.SUCCESS if configured else Palette.WARNING,
        )

    def _on_permissions_changed(self) -> None:
        self.toast.show_message("Permissions updated.",
                                colour=Palette.SUCCESS)

    def _clear_history(self) -> None:
        reply = QMessageBox.question(
            self, "Clear history",
            "Delete the entire saved command history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.app.history.clear()
            self._refresh_history_view()
            self.toast.show_message("History cleared.",
                                    colour=Palette.SUCCESS)

    def _refresh_user_pill(self) -> None:
        name = self.app.memory.user_name or "Guest"
        self.user_pill.setText(f"\U0001F464 {name}")
        self.setWindowTitle(f"JARVIS \u00B7 {name}" if name != "Guest"
                            else "JARVIS")

    def _on_memory_changed(self, key: str, _value: object) -> None:
        """Memory observer — bounces UI work onto the Qt thread."""
        if key in ("user_name", "name"):
            QTimer.singleShot(0, self._refresh_user_pill)

    def _refresh_llm_pill(self) -> None:
        if self.app.llm and self.app.llm.is_configured:
            provider = getattr(self.app.llm, "provider", "openai")
            label = ("\U0001F9E0 Gemini" if provider == "gemini"
                     else "\U0001F9E0 LLM on")
            self.llm_pill.setText(label)
        else:
            self.llm_pill.setText("\U0001F9E0 LLM off")

    # ==================================================================
    # Close handling
    # ==================================================================
    def closeEvent(self, e) -> None:  # noqa: N802
        # If the user clicks the X but we're not actually quitting, hide
        # to background instead. We treat ``self._closing`` as the "yes,
        # really exit" flag.
        if not self._closing:
            self._hide_to_background()
            e.ignore()
            return
        self._stop_listening.set()
        try:
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.overlay.hide()
            self.overlay.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        e.accept()

    def _on_close(self) -> None:
        self._closing = True
        self.close()
        QApplication.instance().quit()


# ---------------------------------------------------------------------------
# Public run helper
# ---------------------------------------------------------------------------
def run_qt_dashboard(
    app: "JarvisApp",
    voice: bool = True,
    wake_word: bool = True,
    start_hidden: bool = False,
) -> int:
    """Bootstrap the Qt application and show the dashboard."""
    qt_app = QApplication.instance()
    created_qt_app = False
    if qt_app is None:
        qt_app = QApplication([])
        created_qt_app = True
    qt_app.setQuitOnLastWindowClosed(False)  # Tray keeps us alive.
    qt_app.setApplicationName("JARVIS")
    qt_app.setStyle("Fusion")

    dashboard = Dashboard(
        app=app, voice_enabled=voice,
        wake_word=wake_word, start_hidden=start_hidden,
    )
    if not start_hidden:
        dashboard.show()

    code = qt_app.exec() if created_qt_app else 0
    return int(code or 0)
