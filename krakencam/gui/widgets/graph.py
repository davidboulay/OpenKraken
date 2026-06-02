"""Real-time multi-series time graph widget.

:class:`TimeSeriesGraph` plots one or more named series against wall-clock
time, with the right edge pinned to "now".  It draws a panel background,
dotted horizontal gridlines with y-axis tick labels, anti-aliased polylines,
legend chips in the top-left and time tick labels along the bottom.

Pure :class:`QPainter`; no external charting dependencies.  All paint maths is
guarded against degenerate inputs (empty series, zero ranges, zero size).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QSizePolicy, QWidget

from krakencam.gui.theme import COLORS

_LOGGER = logging.getLogger(__name__)

_MARGIN_LEFT = 44.0
_MARGIN_RIGHT = 12.0
_MARGIN_TOP = 12.0
_MARGIN_BOTTOM = 22.0
_LEGEND_TOP = 8.0


@dataclass
class _Series:
    """Internal holder for one plotted series."""

    points: list[tuple[float, float]] = field(default_factory=list)
    color: QColor = field(default_factory=lambda: QColor(COLORS["accent"]))


class TimeSeriesGraph(QWidget):
    """A panel that plots several time series sharing one x/y coordinate space."""

    def __init__(
        self,
        y_label: str = "",
        y_min: float | None = None,
        y_max: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._y_label = y_label
        self._fixed_y_min = y_min
        self._fixed_y_max = y_max
        self._window_seconds = 300
        self._series: dict[str, _Series] = {}

        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def set_series(self, name: str, points: list[tuple[float, float]], color: QColor) -> None:
        """Set (or replace) the *points* of the named series and repaint.

        *points* are ``(wall_time, value)`` tuples; ``wall_time`` is a
        ``time.time()`` timestamp stored per point so the x-window can be
        right-aligned to the current time.
        """

        self._series[name] = _Series(points=list(points), color=QColor(color))
        self.update()

    def remove_series(self, name: str) -> None:
        """Remove a series by name (no-op if absent) and repaint."""

        if name in self._series:
            del self._series[name]
            self.update()

    def set_window_seconds(self, seconds: int) -> None:
        """Set the visible x-axis window (last *seconds* up to now)."""

        self._window_seconds = max(1, int(seconds))
        self.update()

    def clear(self) -> None:
        """Remove all series and repaint."""

        self._series.clear()
        self.update()

    # ------------------------------------------------------------------ #
    # Y-range computation                                                #
    # ------------------------------------------------------------------ #
    def _compute_y_range(self) -> tuple[float, float]:
        """Return (y_min, y_max) honouring fixed bounds or auto-ranging."""

        if self._fixed_y_min is not None and self._fixed_y_max is not None:
            lo, hi = float(self._fixed_y_min), float(self._fixed_y_max)
            if hi <= lo:
                hi = lo + 1.0
            return lo, hi

        values: list[float] = []
        for series in self._series.values():
            for _, value in series.points:
                values.append(value)

        if not values:
            lo = 0.0 if self._fixed_y_min is None else float(self._fixed_y_min)
            hi = 1.0 if self._fixed_y_max is None else float(self._fixed_y_max)
            if hi <= lo:
                hi = lo + 1.0
            return lo, hi

        data_min = min(values)
        data_max = max(values)
        if self._fixed_y_min is not None:
            data_min = float(self._fixed_y_min)
        if self._fixed_y_max is not None:
            data_max = float(self._fixed_y_max)

        if data_max <= data_min:
            # Flat series: open up a symmetric band around the value, but honour a
            # fixed bound (e.g. the speed graph pins y_min=0, so the rpm baseline
            # must not dip below 0 when all values are flat / zero).
            pad = max(1.0, abs(data_max) * 0.1)
            lo = (
                float(self._fixed_y_min)
                if self._fixed_y_min is not None
                else data_min - pad
            )
            hi = (
                float(self._fixed_y_max)
                if self._fixed_y_max is not None
                else data_max + pad
            )
            if hi <= lo:
                hi = lo + 1.0
            return lo, hi

        pad = (data_max - data_min) * 0.10
        lo = data_min if self._fixed_y_min is not None else data_min - pad
        hi = data_max if self._fixed_y_max is not None else data_max + pad
        if hi <= lo:
            hi = lo + 1.0
        return lo, hi

    @staticmethod
    def _nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
        """Return a small list of "nice" y tick values within ``[lo, hi]``."""

        span = hi - lo
        if span <= 0 or target < 1:
            return [lo]
        raw = span / target
        magnitude = 10.0 ** (len(str(int(abs(raw)))) - 1) if raw >= 1 else 0.1
        # Choose a 1/2/5 * 10^k step near raw.
        for mult in (1, 2, 5, 10):
            step = mult * magnitude
            if step >= raw:
                break
        ticks: list[float] = []
        start = step * (int(lo / step))
        v = start
        # Guard against pathological steps.
        if step <= 0:
            return [lo, hi]
        guard = 0
        while v <= hi + step * 0.001 and guard < 64:
            if v >= lo - step * 0.001:
                ticks.append(v)
            v += step
            guard += 1
        if not ticks:
            ticks = [lo, hi]
        return ticks

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

        plot = QRectF(
            _MARGIN_LEFT,
            _MARGIN_TOP,
            w - _MARGIN_LEFT - _MARGIN_RIGHT,
            h - _MARGIN_TOP - _MARGIN_BOTTOM,
        )
        if plot.width() <= 1 or plot.height() <= 1:
            painter.end()
            return

        y_lo, y_hi = self._compute_y_range()
        y_span = y_hi - y_lo
        if y_span <= 0:
            y_span = 1.0

        now = time.time()
        x_start = now - self._window_seconds
        x_span = float(self._window_seconds)

        def to_x(t: float) -> float:
            return plot.left() + (t - x_start) / x_span * plot.width()

        def to_y(v: float) -> float:
            return plot.bottom() - (v - y_lo) / y_span * plot.height()

        tick_font = QFont("DejaVu Sans")
        tick_font.setPixelSize(10)
        painter.setFont(tick_font)
        fm = QFontMetrics(tick_font)

        # --- Horizontal gridlines + y tick labels ---------------------
        grid_pen = QPen(QColor(COLORS["border"]))
        grid_pen.setWidthF(1.0)
        grid_pen.setStyle(Qt.PenStyle.DotLine)

        for tick in self._nice_ticks(y_lo, y_hi):
            if tick < y_lo - 1e-6 or tick > y_hi + 1e-6:
                continue
            gy = to_y(tick)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), gy), QPointF(plot.right(), gy))
            painter.setPen(QPen(QColor(COLORS["text_dim"])))
            label = f"{tick:.0f}" if abs(tick) >= 10 else f"{tick:.1f}"
            painter.drawText(
                QRectF(0, gy - 8, _MARGIN_LEFT - 6, 16),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                label,
            )

        # Plot border.
        painter.setPen(QPen(QColor(COLORS["border"])))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(plot)

        # --- X tick labels (-Nm ... 0) --------------------------------
        painter.setPen(QPen(QColor(COLORS["text_dim"])))
        for frac, text in self._x_tick_labels():
            tx = plot.left() + frac * plot.width()
            tw = fm.horizontalAdvance(text)
            painter.drawText(
                QRectF(tx - tw / 2.0, plot.bottom() + 3, tw, _MARGIN_BOTTOM - 4),
                int(Qt.AlignmentFlag.AlignCenter),
                text,
            )

        # --- Series polylines + filled-ish lines ----------------------
        painter.save()
        painter.setClipRect(plot)
        for series in self._series.values():
            if len(series.points) < 1:
                continue
            poly = QPolygonF()
            for t, v in series.points:
                if t < x_start - x_span:  # far off-window left, skip
                    continue
                poly.append(QPointF(to_x(t), to_y(v)))
            if poly.count() == 0:
                continue
            if poly.count() == 1:
                # Draw a single point marker.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(series.color)
                painter.drawEllipse(poly.at(0), 2.5, 2.5)
                continue
            line_pen = QPen(series.color)
            line_pen.setWidthF(2.0)
            line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(line_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(poly)
        painter.restore()

        # --- Legend chips (top-left) ----------------------------------
        self._draw_legend(painter, plot)

        painter.end()

    def _x_tick_labels(self) -> list[tuple[float, str]]:
        """Return ``(fraction, label)`` x ticks: left = ``-Nm``, right = ``0``."""

        win = self._window_seconds
        # Three ticks: window start, midpoint, now.
        def fmt(seconds_ago: float) -> str:
            if seconds_ago <= 0:
                return "0"
            if seconds_ago >= 60:
                mins = seconds_ago / 60.0
                if abs(mins - round(mins)) < 1e-6:
                    return f"-{int(round(mins))}m"
                return f"-{mins:.1f}m"
            return f"-{int(round(seconds_ago))}s"

        return [
            (0.0, fmt(win)),
            (0.5, fmt(win / 2.0)),
            (1.0, fmt(0)),
        ]

    def _draw_legend(self, painter: QPainter, plot: QRectF) -> None:
        """Draw small coloured legend chips along the top-left of the plot."""

        if not self._series:
            return
        legend_font = QFont("DejaVu Sans")
        legend_font.setPixelSize(11)
        legend_font.setBold(True)
        painter.setFont(legend_font)
        fm = QFontMetrics(legend_font)

        x = plot.left() + 6.0
        y = _LEGEND_TOP
        dot_r = 4.0
        gap = 14.0
        for name, series in self._series.items():
            label = name.replace("_", " ")
            text_w = fm.horizontalAdvance(label)
            chip_w = dot_r * 2 + 6 + text_w + 12
            # Wrap if we would overflow the plot width.
            if x + chip_w > plot.right() and x > plot.left() + 6.0:
                break
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(series.color)
            painter.drawEllipse(QPointF(x + dot_r, y + fm.height() / 2.0), dot_r, dot_r)
            painter.setPen(QPen(QColor(COLORS["text"])))
            painter.drawText(
                QRectF(x + dot_r * 2 + 5, y, text_w + 4, fm.height()),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                label,
            )
            x += chip_w + gap
