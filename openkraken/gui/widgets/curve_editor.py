"""Interactive fan/pump duty-curve editor widget.

:class:`CurveEditor` lets the user shape a piecewise-linear duty curve mapping a
temperature (x, deg C) to a duty (y, %).  Points can be dragged with the left
button, added by double-clicking empty space and removed with a right-click
(a minimum of two points is always kept).

The widget renders a grid with axis labels, a translucent accent fill under the
curve and accent point handles with a hover halo.  ``curveChanged`` is emitted
only on a meaningful user edit (mouse-release after a drag, add, or remove) so
listeners are not spammed during a drag.

Pure :class:`QPainter`; no external dependencies beyond PyQt6.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import QSizePolicy, QWidget

from openkraken.gui.theme import COLORS

_LOGGER = logging.getLogger(__name__)

_MARGIN_LEFT = 36.0
_MARGIN_BOTTOM = 36.0
_MARGIN_TOP = 14.0
_MARGIN_RIGHT = 14.0

_POINT_RADIUS = 8.0
_HIT_RADIUS = 12.0


class CurveEditor(QWidget):
    """A draggable piecewise-linear temperature -> duty curve editor."""

    #: Emitted after any committed user edit with the new ``list[(temp, duty)]``.
    curveChanged = pyqtSignal(list)

    def __init__(
        self,
        x_min: float = 20,
        x_max: float = 60,
        y_min: float = 0,
        y_max: float = 100,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._x_min = float(x_min)
        self._x_max = float(x_max)
        self._y_min = float(y_min)
        self._y_max = float(y_max)

        self._points: list[list[float]] = [[x_min, 30.0], [x_max, 100.0]]
        self._y_floor = float(y_min)

        self._drag_index: int | None = None
        self._hover_index: int | None = None
        self._dragged = False  # whether the active drag actually moved the point

        self._live_x: float | None = None
        self._live_y: float | None = None
        self._enabled_visual = True

        self.setMinimumSize(220, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def set_curve(self, points: list[tuple[float, int]]) -> None:
        """Replace the curve points; does **not** emit :data:`curveChanged`."""

        cleaned = self._sanitize(points)
        self._points = [[float(t), float(d)] for t, d in cleaned]
        self._drag_index = None
        self._hover_index = None
        self.update()

    def curve(self) -> list[tuple[float, int]]:
        """Return the current curve as a sorted ``list[(temp, duty)]``."""

        return [(float(p[0]), int(round(p[1]))) for p in self._points]

    def set_y_floor(self, duty: int) -> None:
        """Set the minimum duty a point may be dragged to (e.g. pump = 20)."""

        self._y_floor = max(self._y_min, min(self._y_max, float(duty)))
        # Lift any existing points that violate the new floor.
        changed = False
        for p in self._points:
            if p[1] < self._y_floor:
                p[1] = self._y_floor
                changed = True
        if changed:
            self.update()

    def set_live_marker(self, x: float | None, y: float | None) -> None:
        """Set the current operating point shown as a pulsing marker."""

        self._live_x = None if x is None else float(x)
        self._live_y = None if y is None else float(y)
        self.update()

    def set_enabled_visual(self, enabled: bool) -> None:
        """Dim the widget and ignore interaction when *enabled* is ``False``."""

        self._enabled_visual = bool(enabled)
        self.update()

    # ------------------------------------------------------------------ #
    # Geometry helpers                                                   #
    # ------------------------------------------------------------------ #
    def _plot_rect(self) -> QRectF:
        w = self.width()
        h = self.height()
        return QRectF(
            _MARGIN_LEFT,
            _MARGIN_TOP,
            max(1.0, w - _MARGIN_LEFT - _MARGIN_RIGHT),
            max(1.0, h - _MARGIN_TOP - _MARGIN_BOTTOM),
        )

    def _to_px(self, temp: float, duty: float, plot: QRectF) -> QPointF:
        xs = self._x_max - self._x_min
        ys = self._y_max - self._y_min
        fx = 0.0 if xs <= 0 else (temp - self._x_min) / xs
        fy = 0.0 if ys <= 0 else (duty - self._y_min) / ys
        return QPointF(
            plot.left() + fx * plot.width(),
            plot.bottom() - fy * plot.height(),
        )

    def _from_px(self, px: QPointF, plot: QRectF) -> tuple[float, float]:
        xs = self._x_max - self._x_min
        ys = self._y_max - self._y_min
        fx = 0.0 if plot.width() <= 0 else (px.x() - plot.left()) / plot.width()
        fy = 0.0 if plot.height() <= 0 else (plot.bottom() - px.y()) / plot.height()
        temp = self._x_min + max(0.0, min(1.0, fx)) * xs
        duty = self._y_min + max(0.0, min(1.0, fy)) * ys
        return temp, duty

    def _sanitize(self, points: list[tuple[float, int]]) -> list[tuple[float, float]]:
        """Clamp, dedupe and sort *points*; guarantee at least two of them."""

        cleaned: list[tuple[float, float]] = []
        seen: set[float] = set()
        for t, d in points:
            tt = max(self._x_min, min(self._x_max, float(t)))
            dd = max(self._y_floor, min(self._y_max, float(d)))
            key = round(tt, 6)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append((tt, dd))
        cleaned.sort(key=lambda p: p[0])
        if len(cleaned) < 2:
            base = cleaned[0][1] if cleaned else 50.0
            cleaned = [
                (self._x_min, max(self._y_floor, base)),
                (self._x_max, max(self._y_floor, base)),
            ]
        return cleaned

    def _hit_test(self, pos: QPointF, plot: QRectF) -> int | None:
        """Return the index of the point under *pos*, or ``None``."""

        best: int | None = None
        best_dist = _HIT_RADIUS
        for i, (t, d) in enumerate(self._points):
            px = self._to_px(t, d, plot)
            dist = ((px.x() - pos.x()) ** 2 + (px.y() - pos.y()) ** 2) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best = i
        return best

    # ------------------------------------------------------------------ #
    # Mouse interaction                                                  #
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._enabled_visual:
            return
        plot = self._plot_rect()
        pos = event.position()

        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._hit_test(pos, plot)
            if idx is not None:
                self._drag_index = idx
                self._dragged = False
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            idx = self._hit_test(pos, plot)
            if idx is not None and len(self._points) > 2:
                del self._points[idx]
                self._hover_index = None
                self.update()
                self._emit_changed()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._enabled_visual:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        plot = self._plot_rect()
        pos = event.position()
        if self._hit_test(pos, plot) is not None:
            # Double-clicking on an existing point should not add a new one.
            return
        if not plot.contains(pos):
            return
        temp, duty = self._from_px(pos, plot)
        duty = max(self._y_floor, min(self._y_max, duty))
        self._points.append([temp, duty])
        self._points.sort(key=lambda p: p[0])
        self.update()
        self._emit_changed()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        plot = self._plot_rect()
        pos = event.position()

        if self._drag_index is not None and self._enabled_visual:
            self._move_dragged_point(pos, plot)
            event.accept()
            return

        # Hover handling: change cursor and halo when over a point.
        idx = self._hit_test(pos, plot) if self._enabled_visual else None
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        if self._enabled_visual and idx is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_index is not None:
            was_dragged = self._dragged
            self._drag_index = None
            self._dragged = False
            self.unsetCursor()
            if was_dragged:
                self._emit_changed()
            event.accept()

    def _move_dragged_point(self, pos: QPointF, plot: QRectF) -> None:
        """Move the active point, clamped to the plot, floor and its neighbours."""

        idx = self._drag_index
        if idx is None:
            return
        temp, duty = self._from_px(pos, plot)
        duty = max(self._y_floor, min(self._y_max, duty))

        # Endpoints stay pinned to the x extremes; interior points cannot cross
        # their neighbours (keep the list sorted by x while dragging).
        eps = 0.25
        if idx == 0:
            temp = self._x_min
        elif idx == len(self._points) - 1:
            temp = self._x_max
        else:
            lo = self._points[idx - 1][0] + eps
            hi = self._points[idx + 1][0] - eps
            if hi < lo:
                hi = lo
            temp = max(lo, min(hi, temp))

        self._points[idx][0] = temp
        self._points[idx][1] = duty
        self._dragged = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def _emit_changed(self) -> None:
        self.curveChanged.emit(self.curve())

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

        plot = self._plot_rect()
        if plot.width() <= 1 or plot.height() <= 1:
            painter.end()
            return

        dim = not self._enabled_visual

        self._draw_grid(painter, plot, dim)
        self._draw_curve(painter, plot, dim)
        self._draw_live_marker(painter, plot, dim)
        self._draw_points(painter, plot, dim)

        painter.end()

    def _draw_grid(self, painter: QPainter, plot: QRectF, dim: bool) -> None:
        grid_pen = QPen(QColor(COLORS["border"]))
        grid_pen.setWidthF(1.0)
        grid_pen.setStyle(Qt.PenStyle.DotLine)

        label_color = QColor(COLORS["text_dim"])
        if dim:
            label_color.setAlpha(110)

        font = QFont("DejaVu Sans")
        font.setPixelSize(10)
        painter.setFont(font)
        fm = QFontMetrics(font)

        # Y gridlines / labels (duty %).
        for step in range(0, 101, 25):
            duty = self._y_min + (self._y_max - self._y_min) * (step / 100.0)
            py = self._to_px(self._x_min, duty, plot).y()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), py), QPointF(plot.right(), py))
            painter.setPen(QPen(label_color))
            painter.drawText(
                QRectF(0, py - 8, _MARGIN_LEFT - 5, 16),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                f"{int(round(duty))}",
            )

        # X gridlines / labels (deg C).
        span = self._x_max - self._x_min
        ticks = 4
        for i in range(ticks + 1):
            temp = self._x_min + span * (i / ticks)
            px = self._to_px(temp, self._y_min, plot).x()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(px, plot.top()), QPointF(px, plot.bottom()))
            painter.setPen(QPen(label_color))
            label = f"{int(round(temp))}"
            tw = fm.horizontalAdvance(label)
            painter.drawText(
                QRectF(px - tw / 2.0, plot.bottom() + 4, tw, 14),
                int(Qt.AlignmentFlag.AlignCenter),
                label,
            )

        # Axis unit captions.
        painter.setPen(QPen(label_color))
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 18, plot.width(), 14),
            int(Qt.AlignmentFlag.AlignCenter),
            "Temperature °C",
        )
        painter.save()
        painter.translate(11, plot.center().y())
        painter.rotate(-90)
        painter.drawText(
            QRectF(-plot.height() / 2.0, -10, plot.height(), 12),
            int(Qt.AlignmentFlag.AlignCenter),
            "Duty %",
        )
        painter.restore()

        # Plot border.
        border = QColor(COLORS["border"])
        painter.setPen(QPen(border))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(plot)

    def _curve_polygon(self, plot: QRectF) -> QPolygonF:
        poly = QPolygonF()
        for t, d in self._points:
            poly.append(self._to_px(t, d, plot))
        return poly

    def _draw_curve(self, painter: QPainter, plot: QRectF, dim: bool) -> None:
        if len(self._points) < 2:
            return
        line_poly = self._curve_polygon(plot)

        # Translucent fill under the curve.
        fill_poly = QPolygonF(line_poly)
        fill_poly.append(QPointF(line_poly.at(line_poly.count() - 1).x(), plot.bottom()))
        fill_poly.append(QPointF(line_poly.at(0).x(), plot.bottom()))

        accent = QColor(COLORS["accent"])
        fill = QColor(accent)
        fill.setAlpha(40 if not dim else 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPolygon(fill_poly)

        line_color = QColor(accent)
        if dim:
            line_color.setAlpha(120)
        line_pen = QPen(line_color)
        line_pen.setWidthF(2.0)
        line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(line_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(line_poly)

    def _draw_points(self, painter: QPainter, plot: QRectF, dim: bool) -> None:
        accent = QColor(COLORS["accent"])
        fill = QColor(COLORS["text"])
        if dim:
            accent.setAlpha(120)
            fill.setAlpha(120)

        for i, (t, d) in enumerate(self._points):
            center = self._to_px(t, d, plot)
            if i == self._hover_index and not dim:
                halo = QColor(COLORS["accent"])
                halo.setAlpha(70)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(center, _POINT_RADIUS + 5, _POINT_RADIUS + 5)
            pen = QPen(accent)
            pen.setWidthF(2.5)
            painter.setPen(pen)
            painter.setBrush(fill)
            painter.drawEllipse(center, _POINT_RADIUS / 2.0 + 2.0, _POINT_RADIUS / 2.0 + 2.0)

    def _draw_live_marker(self, painter: QPainter, plot: QRectF, dim: bool) -> None:
        if self._live_x is None or self._live_y is None:
            return
        x = max(self._x_min, min(self._x_max, self._live_x))
        y = max(self._y_min, min(self._y_max, self._live_y))
        center = self._to_px(x, y, plot)

        glow = QColor(COLORS["ok"])
        glow.setAlpha(60 if not dim else 25)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, 9.0, 9.0)

        core = QColor(COLORS["ok"])
        if dim:
            core.setAlpha(120)
        painter.setBrush(core)
        painter.setPen(QPen(QColor(COLORS["bg"])))
        painter.drawEllipse(center, 4.0, 4.0)
