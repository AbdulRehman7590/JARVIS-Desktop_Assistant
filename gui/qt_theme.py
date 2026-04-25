"""Qt theme for the modern PySide6 dashboard.

Defines:
    * :class:`Palette` — the same Aurora colour palette used by the legacy
      Tk dashboard, exposed as plain hex strings so QSS can interpolate
      them.
    * :func:`stylesheet` — a single Qt Style Sheet (QSS) string that
      restyles every widget (window, splitter handle, buttons, inputs,
      list view, tabs, scroll bar, …) to match the JARVIS look. We put
      everything in one place so a future re-skin only touches this file.
    * :class:`StatusDot` — a small QWidget that pulses different colours
      based on the assistant's current state (idle / listening / thinking /
      speaking / error).
    * :class:`IconButton` — a circular icon button with hover lift and
      optional pulse ring (used for Mic and Send).
    * :class:`Chip` — clickable pill used by the quick-action grid.
    * :class:`Toast` — transient notification bubble at the bottom of the
      window.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Palette — Aurora theme
# ---------------------------------------------------------------------------
class Palette:
    BG          = "#0a0f1f"
    BG_ALT      = "#101632"
    PANEL       = "#141c38"
    PANEL_HI    = "#1d2748"
    PANEL_HOT   = "#2a3970"
    BORDER      = "#27345a"
    SASH        = "#3a4a8a"
    SASH_HOT    = "#5cd2e6"

    FG          = "#f5f8ff"
    FG_DIM      = "#b0bbd6"
    FG_MUTED    = "#7480a4"

    ACCENT      = "#5ad6ff"
    ACCENT_HI   = "#9bebff"
    ACCENT_INK  = "#04111e"
    ACCENT_2    = "#b994ff"

    USER        = "#86d0ff"
    JARVIS      = "#ffd089"
    SYSTEM      = "#5ad6ff"
    SUCCESS     = "#78f0a6"
    WARNING     = "#f7d984"
    ERROR       = "#ff8c8c"
    CHAT        = "#d6bcff"

    BUBBLE_USER   = "#1f2c54"
    BUBBLE_JARVIS = "#1a223a"


# ---------------------------------------------------------------------------
# Global stylesheet
# ---------------------------------------------------------------------------
def stylesheet() -> str:
    p = Palette
    return f"""
    QWidget {{
        background-color: {p.BG};
        color: {p.FG};
        font-family: "Segoe UI", "Inter", "SF Pro Display", sans-serif;
        font-size: 11pt;
    }}
    QFrame#Panel, QWidget#Panel {{
        background-color: {p.PANEL};
        border-radius: 12px;
    }}
    QFrame#PanelAlt {{
        background-color: {p.BG_ALT};
        border-radius: 10px;
    }}
    QFrame#Card {{
        background-color: {p.BG_ALT};
        border-radius: 10px;
    }}
    QFrame#TopBar {{
        background-color: {p.PANEL};
        border-bottom: 2px solid {p.ACCENT};
        border-radius: 0px;
    }}
    QFrame#AccentBar {{
        background-color: {p.ACCENT};
        max-height: 2px;
        min-height: 2px;
    }}

    /* Headings & subtle labels */
    QLabel#Heading {{
        color: {p.ACCENT};
        font-weight: 700;
        font-size: 13pt;
        padding: 6px 8px;
    }}
    QLabel#H1 {{
        color: {p.ACCENT};
        font-weight: 800;
        font-size: 19pt;
    }}
    QLabel#Brand {{
        color: {p.ACCENT};
        font-weight: 800;
        font-size: 16pt;
        letter-spacing: 1px;
    }}
    QLabel#Subtle {{
        color: {p.FG_DIM};
        font-size: 10pt;
    }}
    QLabel#StatusText {{
        color: {p.FG_DIM};
        font-size: 11pt;
    }}
    QLabel#Pill {{
        background-color: {p.PANEL_HI};
        color: {p.FG};
        font-weight: 600;
        font-size: 9pt;
        padding: 4px 12px;
        border-radius: 11px;
    }}

    /* Buttons */
    QPushButton {{
        background-color: {p.PANEL_HI};
        color: {p.FG};
        border: 1px solid {p.BORDER};
        border-radius: 8px;
        padding: 8px 16px;
        font-weight: 600;
    }}
    QPushButton:hover  {{ background-color: {p.PANEL_HOT}; }}
    QPushButton:pressed {{ background-color: #1a2244; }}

    QPushButton#Accent {{
        background-color: {p.ACCENT};
        color: {p.ACCENT_INK};
        border: 1px solid {p.ACCENT};
        font-weight: 700;
    }}
    QPushButton#Accent:hover  {{ background-color: {p.ACCENT_HI}; }}
    QPushButton#Accent:pressed {{ background-color: #2bb3b1; }}

    QPushButton#Danger {{
        background-color: #3a1e26;
        color: {p.ERROR};
        border: 1px solid #5a2a35;
    }}
    QPushButton#Danger:hover {{ background-color: #502635; }}

    QPushButton#Ghost {{
        background-color: transparent;
        border: 1px solid {p.BORDER};
        color: {p.FG_DIM};
    }}
    QPushButton#Ghost:hover {{ color: {p.ACCENT}; border-color: {p.ACCENT}; }}

    /* Line edits */
    QLineEdit {{
        background-color: {p.PANEL_HI};
        color: {p.FG};
        border: 1px solid {p.BORDER};
        border-radius: 8px;
        padding: 10px 14px;
        selection-background-color: {p.PANEL_HOT};
    }}
    QLineEdit:focus {{
        border: 1px solid {p.ACCENT};
    }}

    /* Combo box */
    QComboBox {{
        background-color: {p.PANEL_HI};
        color: {p.FG};
        border: 1px solid {p.BORDER};
        border-radius: 8px;
        padding: 8px 14px;
        min-height: 22px;
    }}
    QComboBox:hover {{ border: 1px solid {p.ACCENT}; }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border-left: 1px solid {p.BORDER};
    }}
    QComboBox QAbstractItemView {{
        background-color: {p.PANEL};
        color: {p.FG};
        border: 1px solid {p.BORDER};
        selection-background-color: {p.PANEL_HOT};
        selection-color: {p.FG};
        padding: 4px;
    }}

    /* Plain text edit (transcript) */
    QTextEdit {{
        background-color: {p.PANEL};
        color: {p.FG};
        border: none;
        border-radius: 12px;
        padding: 14px;
        selection-background-color: {p.PANEL_HOT};
    }}

    /* Scroll bars */
    QScrollBar:vertical {{
        background: {p.BG};
        width: 12px;
        margin: 4px 2px 4px 0;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical {{
        background: {p.PANEL_HI};
        min-height: 30px;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.PANEL_HOT}; }}
    QScrollBar::handle:vertical:pressed {{ background: {p.ACCENT}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px; background: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
    QScrollBar:horizontal {{
        background: {p.BG};
        height: 12px;
        margin: 0 4px 2px 4px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal {{
        background: {p.PANEL_HI};
        min-width: 30px;
        border-radius: 6px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {p.PANEL_HOT}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px; background: none;
    }}

    /* Splitter — visible accent-tinted handle */
    QSplitter::handle {{
        background-color: {p.SASH};
    }}
    QSplitter::handle:hover {{
        background-color: {p.SASH_HOT};
    }}
    QSplitter::handle:horizontal {{ width: 6px; }}
    QSplitter::handle:vertical   {{ height: 6px; }}

    /* Tabs */
    QTabWidget::pane {{
        background-color: {p.PANEL};
        border: 1px solid {p.BORDER};
        border-radius: 12px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: {p.PANEL};
        color: {p.FG_DIM};
        padding: 10px 22px;
        margin-right: 4px;
        border-top-left-radius: 10px;
        border-top-right-radius: 10px;
        font-weight: 700;
    }}
    QTabBar::tab:selected {{
        background: {p.PANEL_HI};
        color: {p.ACCENT};
    }}
    QTabBar::tab:hover:!selected {{
        color: {p.FG};
    }}

    /* List widget — used for history */
    QListView, QListWidget {{
        background-color: {p.PANEL};
        color: {p.FG};
        border: none;
        border-radius: 10px;
        padding: 4px;
        outline: 0;
    }}
    QListView::item, QListWidget::item {{
        padding: 8px 10px;
        border-radius: 6px;
        margin: 2px 0;
    }}
    QListView::item:hover, QListWidget::item:hover {{
        background: {p.PANEL_HI};
    }}
    QListView::item:selected, QListWidget::item:selected {{
        background: {p.PANEL_HOT};
        color: {p.ACCENT};
    }}

    /* Headers (used by tab labels above) – fall-through for anything else */
    QHeaderView::section {{
        background: {p.BG};
        color: {p.FG_DIM};
        padding: 6px 8px;
        border: none;
        border-bottom: 1px solid {p.BORDER};
        font-weight: 700;
    }}

    /* Progress bar */
    QProgressBar {{
        background-color: {p.PANEL};
        border: none;
        border-radius: 4px;
        height: 6px;
    }}
    QProgressBar::chunk {{
        background-color: {p.ACCENT};
        border-radius: 4px;
    }}

    /* Group box / radio / check */
    QRadioButton, QCheckBox {{
        spacing: 8px;
        color: {p.FG};
    }}
    QRadioButton::indicator, QCheckBox::indicator {{
        width: 16px; height: 16px;
    }}
    QRadioButton::indicator:unchecked {{
        border: 1px solid {p.FG_MUTED};
        border-radius: 8px;
        background: {p.PANEL};
    }}
    QRadioButton::indicator:checked {{
        border: 1px solid {p.ACCENT};
        border-radius: 8px;
        background: {p.ACCENT};
    }}
    QCheckBox::indicator:unchecked {{
        border: 1px solid {p.FG_MUTED};
        border-radius: 4px;
        background: {p.PANEL};
    }}
    QCheckBox::indicator:checked {{
        border: 1px solid {p.ACCENT};
        border-radius: 4px;
        background: {p.ACCENT};
    }}

    /* Tooltips */
    QToolTip {{
        background-color: {p.PANEL_HOT};
        color: {p.FG};
        border: 1px solid {p.ACCENT};
        padding: 6px 10px;
        border-radius: 6px;
    }}

    /* Scroll areas — keep transparent so panels show through */
    QScrollArea {{
        background-color: transparent;
        border: none;
    }}
    QScrollArea > QWidget > QWidget {{
        background-color: transparent;
    }}
    """


# ---------------------------------------------------------------------------
# StatusDot — pulsing indicator
# ---------------------------------------------------------------------------
class StatusDot(QWidget):
    """Pulses different colours based on the assistant state."""

    SIZE = 30

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._state = "idle"
        self._tick = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._tick = 0
        self.update()

    @property
    def state(self) -> str:
        return self._state

    def _on_tick(self) -> None:
        self._tick += 1
        if self._state != "idle":
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = cy = self.SIZE / 2

        # Static outer border
        painter.setPen(QPen(QColor(Palette.BORDER), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(2, 2, self.SIZE - 4, self.SIZE - 4))

        if self._state == "idle":
            painter.setBrush(QBrush(QColor(Palette.FG_MUTED)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))
            return

        t = self._tick / 8.0
        if self._state == "listening":
            colour = QColor(Palette.ACCENT)
            radius = 9 + 3 * abs(math.sin(t))
        elif self._state == "thinking":
            colour = QColor(Palette.ACCENT_2)
            radius = 9 + 2 * math.sin(t * 1.6)
        elif self._state == "speaking":
            colour = QColor(Palette.JARVIS)
            radius = 9 + 2.5 * abs(math.sin(t * 0.8))
        elif self._state == "error":
            colour = QColor(Palette.ERROR)
            radius = 9
        else:
            colour = QColor(Palette.FG_MUTED)
            radius = 9

        painter.setPen(QPen(colour, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(cx - radius, cy - radius,
                                   radius * 2, radius * 2))
        painter.setBrush(QBrush(colour))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))


# ---------------------------------------------------------------------------
# IconButton — circular icon button with optional pulse ring
# ---------------------------------------------------------------------------
class IconButton(QWidget):
    """Circular flat button with hover-lift and optional pulse animation."""

    clicked = Signal()

    def __init__(
        self,
        glyph: str,
        *,
        palette: str = "accent",
        size: int = 46,
        tooltip: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self._palette = palette
        self._size = size
        self._hover = False
        self._pressed = False
        self._pulse = False
        self._tick = 0

        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)

        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def set_palette(self, palette: str) -> None:
        self._palette = palette
        self.update()

    def set_pulse(self, on: bool) -> None:
        self._pulse = on
        self.update()

    def _on_tick(self) -> None:
        if self._pulse and self._palette == "accent":
            self._tick += 1
            self.update()

    def enterEvent(self, _e) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit()

    def _colours(self) -> tuple[QColor, QColor, QColor]:
        if self._palette == "accent":
            fill = QColor(Palette.ACCENT_HI if self._hover else Palette.ACCENT)
            outline = QColor(Palette.ACCENT)
            text = QColor(Palette.ACCENT_INK)
        elif self._palette == "danger":
            fill = QColor("#502635" if self._hover else Palette.PANEL_HI)
            outline = QColor(Palette.ERROR)
            text = QColor(Palette.ERROR)
        elif self._palette == "muted":
            fill = QColor(Palette.PANEL_HOT if self._hover else Palette.PANEL_HI)
            outline = QColor(Palette.BORDER)
            text = QColor(Palette.FG_DIM)
        else:
            fill = QColor(Palette.PANEL_HOT if self._hover else Palette.PANEL_HI)
            outline = QColor(Palette.BORDER)
            text = QColor(Palette.FG)
        return fill, outline, text

    def paintEvent(self, _e) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._size
        fill, outline, text = self._colours()

        # Pulse ring
        if self._pulse and self._palette == "accent":
            t = self._tick / 6.0
            radius = 4 + 3 * abs(math.sin(t))
            pen = QPen(QColor(Palette.ACCENT), 1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(
                QRectF(2 - radius, 2 - radius,
                       s - 4 + 2 * radius, s - 4 + 2 * radius)
            )

        # Body
        painter.setPen(QPen(outline, 2))
        painter.setBrush(QBrush(fill))
        offset = 1 if self._pressed else 0
        painter.drawEllipse(
            QRectF(2, 2 + offset, s - 4, s - 4)
        )

        # Glyph
        font = QFont("Segoe UI Symbol")
        font.setPointSize(max(10, int(s * 0.36)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(text))
        painter.drawText(
            QRect(0, offset, s, s),
            Qt.AlignmentFlag.AlignCenter,
            self._glyph,
        )


# ---------------------------------------------------------------------------
# Chip — clickable pill used by quick actions
# ---------------------------------------------------------------------------
class Chip(QPushButton):
    """A flat, pill-shaped quick-action button."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Chip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(self._chip_qss())
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(34)

    @staticmethod
    def _chip_qss() -> str:
        p = Palette
        return f"""
        QPushButton#Chip {{
            background-color: {p.PANEL_HI};
            color: {p.FG};
            border: 1px solid {p.BORDER};
            border-radius: 17px;
            padding: 6px 14px;
            font-weight: 700;
        }}
        QPushButton#Chip:hover {{
            background-color: {p.PANEL_HOT};
            color: {p.ACCENT_HI};
            border: 1px solid {p.ACCENT};
        }}
        QPushButton#Chip:pressed {{
            background-color: {p.PANEL};
        }}
        """


# ---------------------------------------------------------------------------
# Toast — transient floating bubble
# ---------------------------------------------------------------------------
class Toast(QFrame):
    """Floating, fading status bubble. Use :meth:`show_message`."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._label = QLabel("", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.addWidget(self._label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)

        # Soft fade-in / fade-out animation.
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade_in = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_in.setDuration(180)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_out_anim = QPropertyAnimation(
            self._opacity, b"opacity", self
        )
        self._fade_out_anim.setDuration(220)
        self._fade_out_anim.setStartValue(1.0)
        self._fade_out_anim.setEndValue(0.0)
        self._fade_out_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out_anim.finished.connect(self.hide)

        self.hide()
        self._apply_style(Palette.ACCENT)

    def _apply_style(self, colour: str) -> None:
        self.setStyleSheet(f"""
        QFrame#Toast {{
            background-color: {Palette.PANEL_HI};
            border: 2px solid {colour};
            border-radius: 14px;
        }}
        QLabel {{
            color: {colour};
            font-weight: 700;
            background: transparent;
        }}
        """)

    def show_message(self, text: str, *, duration_ms: int = 2400,
                     colour: str = Palette.ACCENT) -> None:
        if not text:
            return
        self._apply_style(colour)
        self._label.setText(text)
        self._label.adjustSize()
        self.adjustSize()
        # Anchor to bottom-centre of parent.
        parent = self.parentWidget()
        if parent is not None:
            pw = parent.width()
            ph = parent.height()
            tw = self.width()
            th = self.height()
            self.move(max(0, (pw - tw) // 2), max(0, ph - th - 24))
        self.show()
        self.raise_()
        # Fade in fresh, regardless of any in-flight animation.
        self._fade_out_anim.stop()
        self._fade_in.stop()
        self._opacity.setOpacity(0.0)
        self._fade_in.start()
        self._timer.start(duration_ms)

    def _fade_out(self) -> None:
        self._fade_in.stop()
        self._fade_out_anim.stop()
        self._fade_out_anim.start()


# ---------------------------------------------------------------------------
# Tray icon image — produces a QIcon matching the JARVIS look
# ---------------------------------------------------------------------------
def make_tray_icon(size: int = 64) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(Palette.BG))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(0, 0, size, size))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(Palette.ACCENT), max(2, size // 16)))
    pad = size * 0.12
    p.drawEllipse(QRectF(pad, pad, size - 2 * pad, size - 2 * pad))
    p.setBrush(QColor(Palette.ACCENT))
    p.setPen(Qt.PenStyle.NoPen)
    inner = size * 0.32
    p.drawEllipse(QRectF((size - inner) / 2, (size - inner) / 2,
                          inner, inner))
    p.end()
    return QIcon(pix)
