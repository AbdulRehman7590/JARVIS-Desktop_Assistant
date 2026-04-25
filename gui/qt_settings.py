"""PySide6 Settings dialog (LLM brain + Permissions).

Design philosophy: **minimal**. Two tabs, one column each, generous
whitespace, no decorative sub-headings. Every form row is one label +
one control; the action row at the bottom is sticky.

Sizing rules (per the original spec, kept intact):
    * Default size: 720 x 780.
    * Minimum width  = 720 — the dialog is designed for this width.
    * Minimum height = 780 — but body lives inside a :class:`QScrollArea`
      so shrinking the dialog past 780 surfaces a scrollbar instead of
      clipping anything.

Behavioural notes:
    * Provider preset combo bumps in the right URL + model and writes the
      correct ``LLM_PROVIDER`` so :class:`core.llm_client.LLMClient`
      picks the matching backend.
    * Google Gemini uses the native :mod:`core.gemini_client` SDK path;
      every other preset uses the OpenAI-compatible HTTP backend.
    * Permissions tab is a compact one-row-per-category table with
      coloured status chips and a single "Reset all" button at the
      bottom.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Dict, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.env_manager import env_file_path, get_value, set_value
from core.llm_client import LLMClient, detect_provider
from core.permissions import PermissionCategory
from gui.qt_theme import Palette

if TYPE_CHECKING:  # pragma: no cover
    from main import JarvisApp


_DEFAULT_WIDTH = 720
_DEFAULT_HEIGHT = 780


# (label, base URL, default model, provider key)
PRESET_PROVIDERS = [
    ("OpenAI",           "https://api.openai.com/v1",                                "gpt-4o-mini",                            "openai"),
    ("Google Gemini",    "https://generativelanguage.googleapis.com/v1beta/openai",  "gemini-2.5-flash",                       "gemini"),
    ("OpenRouter",       "https://openrouter.ai/api/v1",                             "openrouter/auto",                        "openai"),
    ("Groq",             "https://api.groq.com/openai/v1",                           "llama-3.1-70b-versatile",                "openai"),
    ("Together",         "https://api.together.xyz/v1",                              "meta-llama/Llama-3.3-70B-Instruct-Turbo","openai"),
    ("Local (Ollama)",   "http://localhost:11434/v1",                                "llama3.1",                               "openai"),
    ("Custom\u2026",     "",                                                         "",                                       "openai"),
]

_PROVIDER_HINTS = {
    "OpenAI":         "platform.openai.com  \u00B7  gpt-4o-mini, gpt-4o",
    "Google Gemini":  "aistudio.google.com  \u00B7  gemini-2.5-flash, gemini-2.5-pro",
    "OpenRouter":     "openrouter.ai  \u00B7  openrouter/auto for the cheapest fit",
    "Groq":           "console.groq.com  \u00B7  llama-3.1-70b-versatile",
    "Together":       "together.ai  \u00B7  many open-weights models",
    "Local (Ollama)": "ollama serve  \u00B7  llama3.1, qwen2.5, \u2026",
    "Custom\u2026":   "Any OpenAI-compatible Chat Completions endpoint.",
}


class _MainThreadSignal(QObject):
    """Helper signal so worker threads can update the UI safely."""

    fired = Signal(str, str)


class SettingsDialog(QDialog):
    """Minimal modal Settings — LLM brain + Permissions."""

    def __init__(
        self,
        parent: QWidget,
        app: "JarvisApp",
        on_llm_saved: Optional[Callable[[bool], None]] = None,
        on_permissions_changed: Optional[Callable[[], None]] = None,
        initial_tab: int = 0,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self._on_llm_saved = on_llm_saved
        self._on_permissions_changed = on_permissions_changed

        self.setWindowTitle("JARVIS \u2014 Settings")
        self.setModal(True)
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)
        self.setMinimumSize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)

        self._status_signal = _MainThreadSignal(self)
        self._status_signal.fired.connect(self._apply_llm_status)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)

        self._build_llm_tab()
        self._build_permissions_tab()

        try:
            self.tabs.setCurrentIndex(initial_tab)
        except Exception:  # noqa: BLE001
            pass

        # Sticky footer — single Close button on the right.
        footer = QHBoxLayout()
        self._footer_status = QLabel("")
        self._footer_status.setObjectName("Subtle")
        footer.addWidget(self._footer_status, 1)
        close = QPushButton("Close")
        close.setObjectName("Accent")
        close.setMinimumWidth(96)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        outer.addLayout(footer)

    # ==================================================================
    # Helpers
    # ==================================================================
    @staticmethod
    def _scrollable_tab(builder: Callable[[QVBoxLayout], None]) -> QWidget:
        """Build a tab whose body is wrapped in a :class:`QScrollArea`.

        ``builder(parent_layout)`` is called once with the inner layout
        so each tab can keep its widget construction declarative.
        """
        outer = QWidget()
        outer_l = QVBoxLayout(outer)
        outer_l.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        outer_l.addWidget(scroll)

        body = QWidget()
        body.setObjectName("Panel")
        scroll.setWidget(body)
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(24, 22, 24, 22)
        body_l.setSpacing(16)
        builder(body_l)
        body_l.addStretch(1)
        return outer

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setObjectName("Subtle")
        font = QFont(lbl.font())
        font.setBold(True)
        font.setPointSize(9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        lbl.setFont(font)
        lbl.setStyleSheet(f"color: {Palette.FG_MUTED};")
        return lbl

    @staticmethod
    def _hairline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setStyleSheet(
            f"background-color: {Palette.BORDER}; max-height: 1px;"
        )
        return line

    # ==================================================================
    # LLM tab — minimal form layout
    # ==================================================================
    def _build_llm_tab(self) -> None:
        def populate(layout: QVBoxLayout) -> None:
            layout.addWidget(self._section_label("LLM Brain"))

            blurb = QLabel(
                "Connect a model. Gemini uses Google's native SDK; "
                "everything else uses the OpenAI-compatible HTTP API."
            )
            blurb.setObjectName("Subtle")
            blurb.setWordWrap(True)
            layout.addWidget(blurb)

            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
            form.setHorizontalSpacing(16)
            form.setVerticalSpacing(10)

            self._preset_combo = QComboBox()
            self._preset_combo.addItems([n for n, *_ in PRESET_PROVIDERS])
            self._preset_combo.currentTextChanged.connect(self._apply_preset)
            form.addRow("Provider", self._preset_combo)

            self._provider_hint = QLabel("")
            self._provider_hint.setObjectName("Subtle")
            self._provider_hint.setWordWrap(True)
            self._provider_hint.setStyleSheet(
                f"color: {Palette.FG_MUTED}; font-size: 9pt;"
            )
            form.addRow("", self._provider_hint)

            key_row = QWidget()
            key_h = QHBoxLayout(key_row)
            key_h.setContentsMargins(0, 0, 0, 0)
            key_h.setSpacing(6)
            self._key_edit = QLineEdit()
            self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._key_edit.setPlaceholderText("Paste your API key")
            key_h.addWidget(self._key_edit, 1)
            self._show_key = QCheckBox("Show")
            self._show_key.toggled.connect(
                lambda on: self._key_edit.setEchoMode(
                    QLineEdit.EchoMode.Normal if on
                    else QLineEdit.EchoMode.Password
                )
            )
            key_h.addWidget(self._show_key)
            form.addRow("API key", key_row)

            self._url_edit = QLineEdit()
            form.addRow("Base URL", self._url_edit)

            self._model_edit = QLineEdit()
            form.addRow("Model", self._model_edit)

            layout.addLayout(form)

            stored = QLabel(f"Saved to {env_file_path()}")
            stored.setObjectName("Subtle")
            stored_font = QFont(stored.font())
            stored_font.setPointSize(8)
            stored.setFont(stored_font)
            stored.setStyleSheet(f"color: {Palette.FG_MUTED};")
            layout.addWidget(stored)

            self._llm_status_label = QLabel("")
            self._llm_status_label.setWordWrap(True)
            self._llm_status_label.setStyleSheet(f"color: {Palette.FG_DIM};")
            layout.addWidget(self._llm_status_label)

            layout.addWidget(self._hairline())

            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            test_btn = QPushButton("Test connection")
            test_btn.clicked.connect(self._test_connection)
            btn_row.addWidget(test_btn)
            btn_row.addStretch(1)
            save_btn = QPushButton("Save")
            save_btn.setObjectName("Accent")
            save_btn.setMinimumWidth(96)
            save_btn.clicked.connect(self._save_llm)
            btn_row.addWidget(save_btn)
            layout.addLayout(btn_row)

            self._load_current_llm()

        tab = self._scrollable_tab(populate)
        self.tabs.addTab(tab, "LLM brain")

    # ------------------------------------------------------------------
    def _load_current_llm(self) -> None:
        provider = detect_provider()
        if provider == "gemini":
            current_key = (get_value("GEMINI_API_KEY")
                           or get_value("GOOGLE_API_KEY")
                           or get_value("OPENAI_API_KEY") or "")
        else:
            current_key = get_value("OPENAI_API_KEY") or ""
        self._key_edit.setText(current_key)
        self._url_edit.setText(get_value("OPENAI_BASE_URL")
                               or "https://api.openai.com/v1")
        self._model_edit.setText(get_value("OPENAI_MODEL") or "gpt-4o-mini")

        cur_url = self._url_edit.text().strip().rstrip("/")
        match_idx = self._preset_combo.count() - 1  # Custom by default.
        for i, (_name, url, _model, _prov) in enumerate(PRESET_PROVIDERS):
            if url and cur_url == url.rstrip("/"):
                match_idx = i
                break
        # Block the signal so we don't overwrite the freshly-loaded
        # values via _apply_preset.
        self._preset_combo.blockSignals(True)
        self._preset_combo.setCurrentIndex(match_idx)
        self._preset_combo.blockSignals(False)
        self._update_provider_hint()

    def _apply_preset(self, name: str) -> None:
        for label, url, model, _prov in PRESET_PROVIDERS:
            if label == name and url:
                self._url_edit.setText(url)
                self._model_edit.setText(model)
                break
        self._update_provider_hint()

    def _update_provider_hint(self) -> None:
        self._provider_hint.setText(
            _PROVIDER_HINTS.get(self._preset_combo.currentText(), "")
        )

    def _provider_for_preset(self) -> str:
        name = self._preset_combo.currentText()
        for label, _url, _model, prov in PRESET_PROVIDERS:
            if label == name:
                return prov
        return "openai"

    def _set_llm_status(self, text: str, colour: str = Palette.FG_DIM) -> None:
        self._status_signal.fired.emit(text, colour)

    def _apply_llm_status(self, text: str, colour: str) -> None:
        self._llm_status_label.setText(text)
        self._llm_status_label.setStyleSheet(f"color: {colour};")
        self._footer_status.setText(text)
        self._footer_status.setStyleSheet(f"color: {colour};")

    def _test_connection(self) -> None:
        key = self._key_edit.text().strip()
        if not key:
            self._set_llm_status("Enter an API key first.", Palette.WARNING)
            return

        # Apply settings to environment temporarily so LLMClient picks
        # them up. We don't persist until "Save" is clicked.
        provider = self._provider_for_preset()
        os.environ["LLM_PROVIDER"] = provider
        if provider == "gemini":
            os.environ["GEMINI_API_KEY"] = key
        os.environ["OPENAI_API_KEY"] = key
        os.environ["OPENAI_BASE_URL"] = self._url_edit.text().strip()
        os.environ["OPENAI_MODEL"] = self._model_edit.text().strip()
        if provider == "gemini":
            os.environ["GEMINI_MODEL"] = self._model_edit.text().strip()

        self._set_llm_status("Testing\u2026", Palette.FG_DIM)

        def worker() -> None:
            try:
                client = LLMClient()
                resp = client.interpret("Just say hi in JSON chat mode.")
            except Exception as exc:  # noqa: BLE001
                self._set_llm_status(f"Test failed: {exc}", Palette.ERROR)
                return
            if resp is None:
                self._set_llm_status(
                    "Couldn't reach the API. Check key, URL, or network.",
                    Palette.ERROR,
                )
            else:
                self._set_llm_status(
                    f"Connected. Model said: {resp.reply[:80]}",
                    Palette.SUCCESS,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _save_llm(self) -> None:
        key = self._key_edit.text().strip()
        url = self._url_edit.text().strip()
        model = self._model_edit.text().strip()
        provider = self._provider_for_preset()
        try:
            set_value("LLM_PROVIDER", provider)
            set_value("OPENAI_API_KEY", key)
            set_value("OPENAI_BASE_URL", url)
            set_value("OPENAI_MODEL", model)
            if provider == "gemini":
                set_value("GEMINI_API_KEY", key)
                set_value("GEMINI_MODEL", model)
        except (OSError, ValueError) as exc:
            self._set_llm_status(f"Save failed: {exc}", Palette.ERROR)
            return

        configured = bool(key)
        self._set_llm_status(
            "Saved. LLM " + ("enabled." if configured else "disabled."),
            Palette.SUCCESS if configured else Palette.WARNING,
        )
        if self._on_llm_saved:
            self._on_llm_saved(configured)

    # ==================================================================
    # Permissions tab — compact one-row-per-category table
    # ==================================================================
    def _build_permissions_tab(self) -> None:
        def populate(layout: QVBoxLayout) -> None:
            layout.addWidget(self._section_label("Permissions"))

            blurb = QLabel(
                "Each category controls a class of actions. "
                "'Ask each time' will prompt again on the next use."
            )
            blurb.setObjectName("Subtle")
            blurb.setWordWrap(True)
            layout.addWidget(blurb)

            snapshot = self.app.permissions.snapshot()
            self._perm_groups: Dict[PermissionCategory, QButtonGroup] = {}
            self._perm_badges: Dict[PermissionCategory, QLabel] = {}

            for idx, cat in enumerate(PermissionCategory):
                if idx > 0:
                    layout.addWidget(self._hairline())

                row = QHBoxLayout()
                row.setSpacing(12)

                label_col = QVBoxLayout()
                label_col.setSpacing(2)
                title = QLabel(cat.value.replace("_", " ").title())
                title_font = QFont(title.font())
                title_font.setBold(True)
                title.setFont(title_font)
                label_col.addWidget(title)
                desc = QLabel(cat.description)
                desc.setObjectName("Subtle")
                desc.setWordWrap(True)
                desc.setStyleSheet(
                    f"color: {Palette.FG_MUTED}; font-size: 9pt;"
                )
                label_col.addWidget(desc)
                row.addLayout(label_col, 1)

                badge = QLabel()
                badge.setSizePolicy(QSizePolicy.Policy.Fixed,
                                    QSizePolicy.Policy.Fixed)
                row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

                radios = QHBoxLayout()
                radios.setSpacing(8)
                group = QButtonGroup(self)
                current = snapshot.get(cat.value, "ask")
                for label, value in (
                    ("Allow", "always"),
                    ("Deny", "never"),
                    ("Ask", "ask"),
                ):
                    rb = QRadioButton(label)
                    rb.setProperty("perm_value", value)
                    if value == current:
                        rb.setChecked(True)
                    group.addButton(rb)
                    rb.toggled.connect(
                        lambda checked, c=cat, v=value:
                            self._on_perm_changed(c, v) if checked else None
                    )
                    radios.addWidget(rb)
                row.addLayout(radios, 0)

                wrapper = QWidget()
                wrapper.setLayout(row)
                layout.addWidget(wrapper)

                self._perm_groups[cat] = group
                self._perm_badges[cat] = badge
                self._refresh_badge(badge, current)

            layout.addWidget(self._hairline())

            reset_row = QHBoxLayout()
            reset_lbl = QLabel("Forget every decision JARVIS has stored.")
            reset_lbl.setObjectName("Subtle")
            reset_lbl.setStyleSheet(
                f"color: {Palette.FG_MUTED}; font-size: 9pt;"
            )
            reset_row.addWidget(reset_lbl, 1)
            reset_btn = QPushButton("Reset all")
            reset_btn.setObjectName("Danger")
            reset_btn.clicked.connect(self._reset_all_permissions)
            reset_row.addWidget(reset_btn)
            layout.addLayout(reset_row)

        tab = self._scrollable_tab(populate)
        self.tabs.addTab(tab, "Permissions")

    # ------------------------------------------------------------------
    def _on_perm_changed(self, category: PermissionCategory, value: str) -> None:
        decision = None if value == "ask" else value
        try:
            self.app.permissions.set_decision(category, decision)
        except ValueError:
            return
        self._refresh_badge(self._perm_badges[category], value)
        if self._on_permissions_changed:
            self._on_permissions_changed()

    def _reset_all_permissions(self) -> None:
        self.app.permissions.reset()
        for cat, group in self._perm_groups.items():
            for btn in group.buttons():
                if btn.property("perm_value") == "ask":
                    btn.setChecked(True)
                    break
            self._refresh_badge(self._perm_badges[cat], "ask")
        if self._on_permissions_changed:
            self._on_permissions_changed()

    @staticmethod
    def _refresh_badge(label: QLabel, choice: str) -> None:
        if choice == "always":
            text, bg, fg = "Granted", "#1c3a25", Palette.SUCCESS
        elif choice == "never":
            text, bg, fg = "Denied",  "#3a1e26", Palette.ERROR
        else:
            text, bg, fg = "Ask",     Palette.PANEL_HI, Palette.FG_DIM
        label.setText(f" {text} ")
        label.setStyleSheet(
            f"background:{bg}; color:{fg}; padding:2px 10px;"
            f" border-radius:9px; font-weight:700; font-size:9pt;"
        )
