"""Application-wide theming for Kraken CAM.

Provides the colour palette, a comprehensive dark QSS stylesheet, per-metric
graph colours, and QPainter-drawn application / tray icons.

Nothing in this module touches hardware; it only deals with presentation.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Palette                                                                      #
# --------------------------------------------------------------------------- #

#: The single source of truth for colours used across the whole GUI.
COLORS: dict[str, str] = {
    "bg": "#0d0e12",
    "panel": "#16181f",
    "panel2": "#1d2029",
    "border": "#2a2d39",
    "text": "#ecedf1",
    "text_dim": "#8b8e98",
    "accent": "#7c3aed",
    "accent_hover": "#8f55f0",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "crit": "#ef4444",
    "cpu": "#38bdf8",
    "gpu": "#34d399",
    "liquid": "#7c3aed",
    "pump": "#f472b6",
    "fan": "#fbbf24",
}

#: Mapping of dashboard metric names to palette keys for graph series colours.
_METRIC_COLOR_KEYS: dict[str, str] = {
    "liquid_temp": "liquid",
    "cpu_temp": "cpu",
    "gpu_temp": "gpu",
    "pump_rpm": "pump",
    "fan_rpm": "fan",
    "pump_duty": "pump",
    "fan_duty": "fan",
    "cpu_load": "cpu",
    "gpu_load": "gpu",
}


def series_color(metric: str) -> QColor:
    """Return the canonical :class:`QColor` for a dashboard *metric* name.

    Unknown metrics fall back to the accent colour so callers never receive an
    invalid colour.
    """

    key = _METRIC_COLOR_KEYS.get(metric, "accent")
    return QColor(COLORS[key])


# --------------------------------------------------------------------------- #
# Stylesheet                                                                   #
# --------------------------------------------------------------------------- #


def _build_qss() -> str:
    """Build the application QSS string from :data:`COLORS`."""

    c = COLORS
    return f"""
    QWidget {{
        background-color: {c['bg']};
        color: {c['text']};
        font-family: "Inter", "Segoe UI", "DejaVu Sans", sans-serif;
        font-size: 13px;
    }}

    QMainWindow, QDialog {{
        background-color: {c['bg']};
    }}

    /* ---- Panels / frames -------------------------------------------- */
    QFrame#Panel, QWidget#Panel {{
        background-color: {c['panel']};
        border: 1px solid {c['border']};
        border-radius: 10px;
    }}

    QScrollArea {{
        background-color: transparent;
        border: none;
    }}
    QScrollArea > QWidget > QWidget {{
        background-color: transparent;
    }}

    QScrollBar:vertical {{
        background: {c['panel']};
        width: 10px;
        margin: 0px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical {{
        background: {c['border']};
        min-height: 28px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c['accent']};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background: {c['panel']};
        height: 10px;
        margin: 0px;
        border-radius: 5px;
    }}
    QScrollBar::handle:horizontal {{
        background: {c['border']};
        min-width: 28px;
        border-radius: 5px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {c['accent']};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}

    /* ---- Buttons ---------------------------------------------------- */
    QPushButton {{
        background-color: {c['panel2']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 7px 14px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background-color: {c['accent_hover']};
        border-color: {c['accent_hover']};
        color: #ffffff;
    }}
    QPushButton:pressed {{
        background-color: {c['accent']};
        border-color: {c['accent']};
    }}
    QPushButton:checked {{
        background-color: {c['accent']};
        border-color: {c['accent']};
        color: #ffffff;
    }}
    QPushButton:disabled {{
        background-color: {c['panel']};
        color: {c['text_dim']};
        border-color: {c['border']};
    }}

    /* ---- Sidebar navigation buttons --------------------------------- */
    QPushButton[sidebar="true"] {{
        background-color: transparent;
        border: none;
        border-left: 3px solid transparent;
        border-radius: 0px;
        padding: 0px 18px;
        min-height: 44px;
        max-height: 44px;
        text-align: left;
        font-size: 14px;
        font-weight: 600;
        color: {c['text_dim']};
    }}
    QPushButton[sidebar="true"]:hover {{
        background-color: {c['panel']};
        color: {c['text']};
    }}
    QPushButton[sidebar="true"]:checked {{
        background-color: {c['panel2']};
        border-left: 3px solid {c['accent']};
        color: {c['text']};
    }}

    /* ---- ComboBox --------------------------------------------------- */
    QComboBox {{
        background-color: {c['panel2']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 6px 12px;
        min-height: 18px;
    }}
    QComboBox:hover {{
        border-color: {c['accent']};
    }}
    QComboBox:disabled {{
        color: {c['text_dim']};
        background-color: {c['panel']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0px;
        height: 0px;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid {c['text_dim']};
        margin-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {c['panel2']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        selection-background-color: {c['accent']};
        selection-color: #ffffff;
        outline: none;
        padding: 4px;
    }}

    /* ---- Sliders ---------------------------------------------------- */
    QSlider::groove:horizontal {{
        height: 6px;
        background: {c['panel2']};
        border-radius: 3px;
    }}
    QSlider::sub-page:horizontal {{
        background: {c['accent']};
        border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        background: {c['text']};
        width: 16px;
        height: 16px;
        margin: -6px 0;
        border-radius: 8px;
        border: 2px solid {c['accent']};
    }}
    QSlider::handle:horizontal:hover {{
        background: {c['accent_hover']};
    }}
    QSlider::groove:vertical {{
        width: 6px;
        background: {c['panel2']};
        border-radius: 3px;
    }}
    QSlider::add-page:vertical {{
        background: {c['accent']};
        border-radius: 3px;
    }}
    QSlider::handle:vertical {{
        background: {c['text']};
        width: 16px;
        height: 16px;
        margin: 0 -6px;
        border-radius: 8px;
        border: 2px solid {c['accent']};
    }}

    /* ---- Tabs ------------------------------------------------------- */
    QTabWidget::pane {{
        border: 1px solid {c['border']};
        border-radius: 8px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: {c['panel']};
        color: {c['text_dim']};
        border: 1px solid {c['border']};
        border-bottom: none;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        padding: 8px 16px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {c['panel2']};
        color: {c['text']};
        border-bottom: 2px solid {c['accent']};
    }}
    QTabBar::tab:hover {{
        color: {c['text']};
    }}

    /* ---- Group boxes ------------------------------------------------ */
    QGroupBox {{
        background-color: {c['panel']};
        border: 1px solid {c['border']};
        border-radius: 10px;
        margin-top: 14px;
        padding: 12px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        top: 2px;
        padding: 0 6px;
        color: {c['text_dim']};
    }}

    /* ---- Line / spin inputs ----------------------------------------- */
    QLineEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {c['panel2']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 8px;
        padding: 6px 10px;
        selection-background-color: {c['accent']};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {c['accent']};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
        color: {c['text_dim']};
        background-color: {c['panel']};
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        background-color: {c['panel']};
        border: none;
        width: 16px;
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: none;
        width: 0px; height: 0px;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 5px solid {c['text_dim']};
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: none;
        width: 0px; height: 0px;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {c['text_dim']};
    }}

    /* ---- Check / radio ---------------------------------------------- */
    QCheckBox, QRadioButton {{
        spacing: 8px;
        color: {c['text']};
        background: transparent;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {c['border']};
        background: {c['panel2']};
    }}
    QCheckBox::indicator {{
        border-radius: 4px;
    }}
    QRadioButton::indicator {{
        border-radius: 8px;
    }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {c['accent']};
    }}
    QCheckBox::indicator:checked {{
        background: {c['accent']};
        border-color: {c['accent']};
    }}
    QRadioButton::indicator:checked {{
        background: {c['accent']};
        border: 4px solid {c['panel2']};
    }}

    /* ---- Tooltip ---------------------------------------------------- */
    QToolTip {{
        background-color: {c['panel2']};
        color: {c['text']};
        border: 1px solid {c['accent']};
        border-radius: 6px;
        padding: 4px 8px;
    }}

    /* ---- Status bar ------------------------------------------------- */
    QStatusBar {{
        background-color: {c['panel']};
        color: {c['text_dim']};
        border-top: 1px solid {c['border']};
    }}
    QStatusBar::item {{
        border: none;
    }}

    /* ---- Labels ----------------------------------------------------- */
    QLabel {{
        background: transparent;
        color: {c['text']};
    }}
    QLabel[dim="true"] {{
        color: {c['text_dim']};
    }}
    """


def apply_theme(app: QApplication) -> None:
    """Apply the Fusion style, a dark :class:`QPalette` and the QSS stylesheet."""

    app.setStyle("Fusion")

    palette = QPalette()
    bg = QColor(COLORS["bg"])
    panel = QColor(COLORS["panel"])
    panel2 = QColor(COLORS["panel2"])
    text = QColor(COLORS["text"])
    text_dim = QColor(COLORS["text_dim"])
    accent = QColor(COLORS["accent"])

    palette.setColor(QPalette.ColorRole.Window, bg)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, panel)
    palette.setColor(QPalette.ColorRole.AlternateBase, panel2)
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel2)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, panel2)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, accent)
    palette.setColor(QPalette.ColorRole.PlaceholderText, text_dim)

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.Text, text_dim)
    palette.setColor(disabled, QPalette.ColorRole.WindowText, text_dim)
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, text_dim)

    app.setPalette(palette)
    app.setStyleSheet(_build_qss())
    _LOGGER.debug("Theme applied (Fusion + dark palette + QSS)")


# --------------------------------------------------------------------------- #
# Icons                                                                        #
# --------------------------------------------------------------------------- #


def _droplet_path(rect: QRectF) -> QPainterPath:
    """Build a teardrop / water-droplet :class:`QPainterPath` inside *rect*.

    The droplet has a pointed top and a round bottom, centred horizontally.
    """

    path = QPainterPath()
    cx = rect.center().x()
    top = rect.top()
    bottom = rect.bottom()
    half_w = rect.width() / 2.0

    # Start at the pointed apex.
    path.moveTo(cx, top)
    # Right side curving out to the widest point then down into the bowl.
    path.cubicTo(
        QPointF(cx + half_w * 0.55, top + rect.height() * 0.30),
        QPointF(cx + half_w, top + rect.height() * 0.55),
        QPointF(cx + half_w, top + rect.height() * 0.68),
    )
    # Round bottom (right -> bottom -> left).
    path.cubicTo(
        QPointF(cx + half_w, bottom),
        QPointF(cx - half_w, bottom),
        QPointF(cx - half_w, top + rect.height() * 0.68),
    )
    # Left side back up to the apex.
    path.cubicTo(
        QPointF(cx - half_w, top + rect.height() * 0.55),
        QPointF(cx - half_w * 0.55, top + rect.height() * 0.30),
        QPointF(cx, top),
    )
    path.closeSubpath()
    return path


def make_app_icon() -> QIcon:
    """Return a 64 px purple droplet on a dark rounded square as the app icon."""

    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Rounded dark background tile.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(COLORS["panel2"])))
    painter.drawRoundedRect(QRectF(2, 2, size - 4, size - 4), 14, 14)

    # Filled droplet with a vertical purple gradient for a glossy look.
    drop_rect = QRectF(18, 12, size - 36, size - 22)
    grad = QLinearGradient(drop_rect.topLeft(), drop_rect.bottomLeft())
    grad.setColorAt(0.0, QColor(COLORS["accent_hover"]))
    grad.setColorAt(1.0, QColor(COLORS["accent"]))
    painter.setBrush(QBrush(grad))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(_droplet_path(drop_rect))

    # A soft highlight glint.
    painter.setBrush(QBrush(QColor(255, 255, 255, 70)))
    painter.drawEllipse(QPointF(drop_rect.center().x() - 5, drop_rect.center().y() + 4), 4.0, 6.0)

    painter.end()
    return QIcon(pix)


def make_tray_icon(temp: float | None) -> QIcon:
    """Return a 64 px tray icon: a droplet outline with the integer *temp* inside.

    When *temp* is ``None`` only the droplet outline is drawn (no number).
    The text carries no unit and is sized to remain legible at panel resolution.
    """

    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    drop_rect = QRectF(8, 4, size - 16, size - 8)
    path = _droplet_path(drop_rect)

    # Faintly fill the droplet so the outline reads on any panel colour.
    painter.setBrush(QBrush(QColor(124, 58, 237, 60)))
    pen = QPen(QColor(COLORS["accent"]))
    pen.setWidthF(4.0)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPath(path)

    if temp is not None:
        text = str(int(round(temp)))
        # Choose the largest bold font that fits within the droplet's bowl.
        bowl = QRectF(
            drop_rect.left() + 4,
            drop_rect.top() + drop_rect.height() * 0.40,
            drop_rect.width() - 8,
            drop_rect.height() * 0.55,
        )
        font = QFont("DejaVu Sans")
        font.setBold(True)
        point = 34
        font.setPixelSize(point)
        fm = QFontMetrics(font)
        while point > 10 and (
            fm.horizontalAdvance(text) > bowl.width() or fm.height() > bowl.height()
        ):
            point -= 2
            font.setPixelSize(point)
            fm = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(QPen(QColor(COLORS["text"])))
        painter.drawText(bowl, int(Qt.AlignmentFlag.AlignCenter), text)

    painter.end()
    return QIcon(pix)
