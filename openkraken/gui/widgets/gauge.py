"""Circular-arc gauge tile widget.

A :class:`GaugeTile` draws a 270-degree arc gauge with a title above, a large
value in the centre and a smaller sub-line below.  The arc colour switches to
the warn / crit colours once the value passes the configured thresholds.

Pure :class:`QPainter` rendering; no external charting dependencies.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from openkraken.gui.theme import COLORS

_LOGGER = logging.getLogger(__name__)

# Arc geometry: a 270-degree sweep starting at the bottom-left.
# Qt angles are measured in 1/16 degree, counter-clockwise from 3 o'clock.
_ARC_START_DEG = 225.0      # visual start (bottom-left)
_ARC_SPAN_DEG = -270.0      # clockwise sweep to bottom-right
_PEN_WIDTH = 10.0


class GaugeTile(QWidget):
    """A single circular-arc gauge with title, centre value and sub-line."""

    def __init__(
        self,
        title: str,
        unit: str,
        vmin: float,
        vmax: float,
        color: QColor,
        warn: float | None = None,
        crit: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._base_color = QColor(color)
        self._warn = warn
        self._crit = crit

        self._value: float | None = None
        self._sub_text: str = ""

        self.setMinimumSize(150, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def set_value(self, value: float | None, sub_text: str = "") -> None:
        """Update the displayed *value* (``None`` shows ``"--"`` and a dim arc)."""

        self._value = None if value is None else float(value)
        self._sub_text = sub_text
        self.update()

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _fraction(self) -> float:
        """Return the value as a 0..1 fraction of the gauge range (clamped)."""

        span = self._vmax - self._vmin
        if span <= 0 or self._value is None:
            return 0.0
        frac = (self._value - self._vmin) / span
        return max(0.0, min(1.0, frac))

    def _arc_color(self) -> QColor:
        """Pick the arc colour according to warn / crit thresholds."""

        if self._value is None:
            return QColor(COLORS["border"])
        if self._crit is not None and self._value >= self._crit:
            return QColor(COLORS["crit"])
        if self._warn is not None and self._value >= self._warn:
            return QColor(COLORS["warn"])
        return QColor(self._base_color)

    # ------------------------------------------------------------------ #
    # Painting                                                           #
    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:  # noqa: D401, N802 (Qt signature)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            painter.end()
            return

        # Panel background.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["panel"]))
        painter.drawRoundedRect(QRectF(0, 0, w, h), 10, 10)

        # --- Title -----------------------------------------------------
        title_font = QFont("DejaVu Sans")
        title_font.setPixelSize(13)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor(COLORS["text_dim"])))
        title_rect = QRectF(0, 8, w, 20)
        painter.drawText(title_rect, int(Qt.AlignmentFlag.AlignCenter), self._title)

        # --- Arc geometry ---------------------------------------------
        margin = _PEN_WIDTH / 2.0 + 14.0
        # Reserve vertical space for title (top) and sub-line (bottom).
        top = 30.0
        bottom_reserve = 24.0
        avail_w = w - 2 * margin
        avail_h = h - top - bottom_reserve - margin
        diameter = max(10.0, min(avail_w, avail_h))
        arc_x = (w - diameter) / 2.0
        arc_y = top + (avail_h - diameter) / 2.0 + margin / 2.0
        arc_rect = QRectF(arc_x, arc_y, diameter, diameter)

        # Track (full 270-degree arc, dim).
        track_pen = QPen(QColor(COLORS["panel2"]))
        track_pen.setWidthF(_PEN_WIDTH)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(
            arc_rect,
            int(_ARC_START_DEG * 16),
            int(_ARC_SPAN_DEG * 16),
        )

        # Value arc.
        frac = self._fraction()
        if self._value is not None and frac > 0.0:
            value_pen = QPen(self._arc_color())
            value_pen.setWidthF(_PEN_WIDTH)
            value_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(value_pen)
            painter.drawArc(
                arc_rect,
                int(_ARC_START_DEG * 16),
                int(_ARC_SPAN_DEG * frac * 16),
            )

        # --- Centre value ---------------------------------------------
        if self._value is None:
            value_text = "--"
        elif abs(self._value - round(self._value)) < 1e-6:
            value_text = f"{int(round(self._value))}"
        else:
            value_text = f"{self._value:.1f}"

        value_font = QFont("DejaVu Sans")
        value_font.setBold(True)
        value_font.setPixelSize(max(18, int(diameter * 0.30)))
        painter.setFont(value_font)
        painter.setPen(
            QPen(QColor(COLORS["text"] if self._value is not None else COLORS["text_dim"]))
        )
        painter.drawText(arc_rect, int(Qt.AlignmentFlag.AlignCenter), value_text)

        # Unit, drawn small just under the value.
        if self._unit and self._value is not None:
            unit_font = QFont("DejaVu Sans")
            unit_font.setPixelSize(max(10, int(diameter * 0.11)))
            painter.setFont(unit_font)
            painter.setPen(QPen(QColor(COLORS["text_dim"])))
            unit_rect = QRectF(
                arc_rect.left(),
                arc_rect.center().y() + diameter * 0.18,
                arc_rect.width(),
                diameter * 0.16,
            )
            painter.drawText(unit_rect, int(Qt.AlignmentFlag.AlignCenter), self._unit)

        # --- Sub-line --------------------------------------------------
        if self._sub_text:
            sub_font = QFont("DejaVu Sans")
            sub_font.setPixelSize(12)
            painter.setFont(sub_font)
            painter.setPen(QPen(QColor(COLORS["text_dim"])))
            sub_rect = QRectF(6, h - bottom_reserve, w - 12, bottom_reserve - 4)
            painter.drawText(sub_rect, int(Qt.AlignmentFlag.AlignCenter), self._sub_text)

        painter.end()
