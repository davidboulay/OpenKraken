"""Dashboard page: live gauges plus temperature and speed history graphs.

Shows five :class:`~openkraken.gui.widgets.gauge.GaugeTile` instances fed from the
engine's ``sample_ready`` signal, and two
:class:`~openkraken.gui.widgets.graph.TimeSeriesGraph` widgets re-fed from
``engine.history.series()`` on every sample. Metric visibility is controlled by
checkboxes and the time window by a shared combo box.

All slots are resilient to ``None`` values (startup / disconnected state).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from openkraken.gui import theme
from openkraken.gui.widgets.gauge import GaugeTile
from openkraken.gui.widgets.graph import TimeSeriesGraph
from openkraken.gui.widgets.wrap_grid import WrapGrid

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids hardware imports
    from openkraken.backend.device import DeviceStatus
    from openkraken.backend.engine import ControlEngine
    from openkraken.backend.sensors import SystemSnapshot
    from openkraken.config import AppConfig

_LOGGER = logging.getLogger(__name__)

# Window options shared by both graphs: human label -> seconds.
_WINDOWS: list[tuple[str, int]] = [("1m", 60), ("5m", 300), ("10m", 600)]

# Metrics plotted on the temperature graph (metric name -> checkbox label).
_TEMP_METRICS: list[tuple[str, str]] = [
    ("liquid_temp", "Liquid"),
    ("cpu_temp", "CPU"),
    ("gpu_temp", "GPU"),
]

# Metrics plotted on the speed graph.
_SPEED_METRICS: list[tuple[str, str]] = [
    ("pump_rpm", "Pump"),
    ("fan_rpm", "Fan"),
]

# Narrowest width a gauge may be squeezed to before the row wraps. Below roughly
# this the arc and its value stop being readable, so moving to another row is a
# better trade than shrinking further.
_GAUGE_MIN_WIDTH = 120

# Same idea for the Display row: wide enough for the longest checkbox label
# ("Liquid") and for the window picker to sit whole.
_CONTROL_MIN_WIDTH = 96


def _fmt(value: float | int | None, fmt: str = "{:.0f}") -> str:
    """Format a possibly-``None`` numeric value, falling back to ``"--"``."""
    if value is None:
        return "--"
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return "--"


class DashboardPage(QWidget):
    """Live monitoring page with gauges and history graphs."""

    def __init__(
        self,
        engine: "ControlEngine",
        config: "AppConfig",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config

        # Default window is the largest available that does not exceed the
        # configured history; otherwise the smallest.
        self._window_seconds = _WINDOWS[1][1]

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        root.addWidget(self._build_gauges())
        root.addWidget(self._build_controls())
        root.addLayout(self._build_graphs(), stretch=1)

        # Listen for samples. The signal carries (DeviceStatus, SystemSnapshot)
        # as plain objects (pyqtSignal(object, object)).
        try:
            self._engine.sample_ready.connect(self._on_sample)
        except Exception:  # pragma: no cover - defensive against stub engines
            _LOGGER.exception("could not connect sample_ready signal")

    # ------------------------------------------------------------------ build
    def _build_gauges(self) -> QWidget:
        # A wrapping grid, not a QHBoxLayout: five gauges in one unbreakable row
        # pinned the page to an 830 px floor, and a tiling compositor happily
        # allocates less than that, at which point the row overflowed the window
        # instead of re-flowing. _GAUGE_MIN_WIDTH decides when it wraps.
        row = WrapGrid(_GAUGE_MIN_WIDTH, spacing=12)

        self._g_cpu = GaugeTile(
            "CPU", "°C", 0, 100, theme.series_color("cpu_temp"), warn=75, crit=88
        )
        self._g_gpu = GaugeTile(
            "GPU", "°C", 0, 100, theme.series_color("gpu_temp"), warn=85, crit=100
        )
        self._g_liquid = GaugeTile(
            "Liquid", "°C", 20, 60, theme.series_color("liquid_temp"), warn=42, crit=50
        )
        self._g_pump = GaugeTile(
            "Pump", "rpm", 0, 3600, theme.series_color("pump_rpm")
        )
        self._g_fan = GaugeTile("Fan", "rpm", 0, 2400, theme.series_color("fan_rpm"))

        for gauge in (
            self._g_cpu,
            self._g_gpu,
            self._g_liquid,
            self._g_pump,
            self._g_fan,
        ):
            row.add_widget(gauge)
        return row

    def _build_controls(self) -> QWidget:
        box = QGroupBox("Display")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(12, 10, 12, 10)

        # Same reasoning as the gauges: one checkbox per squeezed grid column
        # sheared the labels ("Liquid" -> "Li") once the window went narrow.
        # Wrapping keeps every label whole instead.
        row = WrapGrid(_CONTROL_MIN_WIDTH, spacing=10)

        # Per-metric checkboxes; checking/unchecking toggles series visibility.
        self._checks: dict[str, QCheckBox] = {}
        for metric, label in _TEMP_METRICS + _SPEED_METRICS:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.toggled.connect(self._refresh_graphs)
            self._checks[metric] = cb
            row.add_widget(cb)

        # The window picker travels as a single item so its label can never
        # wrap away from its combo box.
        picker = QWidget()
        picker_row = QHBoxLayout(picker)
        picker_row.setContentsMargins(0, 0, 0, 0)
        picker_row.setSpacing(6)
        picker_row.addWidget(QLabel("Window"))
        self._window_combo = QComboBox()
        for label, _seconds in _WINDOWS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentIndex(1)
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)
        picker_row.addWidget(self._window_combo)
        row.add_widget(picker)

        outer.addWidget(row)
        return box

    def _build_graphs(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(12)

        self._temp_graph = TimeSeriesGraph(y_label="°C", y_min=20, y_max=100)
        self._temp_graph.set_window_seconds(self._window_seconds)
        self._speed_graph = TimeSeriesGraph(y_label="rpm", y_min=0)
        self._speed_graph.set_window_seconds(self._window_seconds)

        col.addWidget(self._temp_graph, stretch=1)
        col.addWidget(self._speed_graph, stretch=1)
        return col

    # ------------------------------------------------------------------ slots
    def _on_window_changed(self, index: int) -> None:
        if 0 <= index < len(_WINDOWS):
            self._window_seconds = _WINDOWS[index][1]
            self._temp_graph.set_window_seconds(self._window_seconds)
            self._speed_graph.set_window_seconds(self._window_seconds)
            self._refresh_graphs()

    def _on_sample(self, status: "DeviceStatus", snap: "SystemSnapshot") -> None:
        """Update gauges from the latest sample then re-feed the graphs."""
        self._update_gauges(status, snap)
        self._refresh_graphs()

    def _update_gauges(
        self, status: "DeviceStatus | None", snap: "SystemSnapshot | None"
    ) -> None:
        cpu_temp = getattr(snap, "cpu_temp", None) if snap is not None else None
        cpu_load = getattr(snap, "cpu_load", None) if snap is not None else None
        cpu_freq = getattr(snap, "cpu_freq_mhz", None) if snap is not None else None
        gpu_temp = getattr(snap, "gpu_temp", None) if snap is not None else None
        gpu_load = getattr(snap, "gpu_load", None) if snap is not None else None
        gpu_power = getattr(snap, "gpu_power_w", None) if snap is not None else None

        liquid = getattr(status, "liquid_temp", None) if status is not None else None
        pump_rpm = getattr(status, "pump_rpm", None) if status is not None else None
        pump_duty = getattr(status, "pump_duty", None) if status is not None else None
        fan_rpm = getattr(status, "fan_rpm", None) if status is not None else None
        fan_duty = getattr(status, "fan_duty", None) if status is not None else None

        # CPU sub-line: "load% @ GHz".
        cpu_sub = f"{_fmt(cpu_load)}%"
        if cpu_freq is not None:
            cpu_sub += f" @ {cpu_freq / 1000.0:.1f} GHz"
        self._g_cpu.set_value(cpu_temp, cpu_sub)

        # GPU sub-line: "load% · power W".
        gpu_sub = f"{_fmt(gpu_load)}%"
        if gpu_power is not None:
            gpu_sub += f" · {gpu_power:.0f} W"
        self._g_gpu.set_value(gpu_temp, gpu_sub)

        # Liquid sub-line shows pump duty (the firmware drives off liquid temp).
        self._g_liquid.set_value(liquid, f"pump {_fmt(pump_duty)}%")
        self._g_pump.set_value(pump_rpm, f"{_fmt(pump_duty)}%")
        self._g_fan.set_value(fan_rpm, f"{_fmt(fan_duty)}%")

    def _refresh_graphs(self) -> None:
        """Pull each series from the engine history and apply checkbox filters."""
        history = getattr(self._engine, "history", None)
        if history is None:
            return

        self._apply_series(self._temp_graph, _TEMP_METRICS, history)
        self._apply_series(self._speed_graph, _SPEED_METRICS, history)

    def _apply_series(
        self,
        graph: TimeSeriesGraph,
        metrics: list[tuple[str, str]],
        history: object,
    ) -> None:
        for metric, _label in metrics:
            cb = self._checks.get(metric)
            if cb is not None and not cb.isChecked():
                graph.remove_series(metric)
                continue
            try:
                points = history.series(metric)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("history.series(%s) failed", metric, exc_info=True)
                points = []
            graph.set_series(metric, points, theme.series_color(metric))
