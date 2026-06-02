"""Cooling page: per-channel pump and fan control.

Two side-by-side :class:`ChannelPanel` group boxes (Pump / Fan). Each panel offers
a profile combo (Silent / Balanced / Performance / Fixed / Custom), a curve source
combo (Liquid / CPU / GPU temp), a :class:`~openkraken.gui.widgets.curve_editor.CurveEditor`
for curve mode and a slider+spinbox for fixed mode. A live operating-point marker is
fed from the engine's ``sample_ready`` signal.

Selecting a preset loads its points into the editor; editing the curve flips the
profile combo to *Custom* (signals are blocked while loading presets to avoid
feedback loops). *Apply* collects a :class:`~openkraken.config.ChannelConfig` and
calls ``engine.apply_channel`` then ``config.save()``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from openkraken.backend import curves
from openkraken.config import ChannelConfig
from openkraken.gui.widgets.curve_editor import CurveEditor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openkraken.backend.device import DeviceStatus
    from openkraken.backend.engine import ControlEngine
    from openkraken.backend.sensors import SystemSnapshot
    from openkraken.config import AppConfig

_LOGGER = logging.getLogger(__name__)

# Profile combo entries: stored name -> human label.
_PROFILES: list[tuple[str, str]] = [
    ("silent", "Silent"),
    ("balanced", "Balanced"),
    ("performance", "Performance"),
    ("fixed", "Fixed"),
    ("custom", "Custom"),
]

# Source combo entries: stored value -> human label.
_SOURCES: list[tuple[str, str]] = [
    ("liquid", "Liquid temp"),
    ("cpu", "CPU temp"),
    ("gpu", "GPU temp"),
]

_FOOTER_NOTE = (
    "Liquid-temp curves run in cooler firmware and persist after this app closes."
)


def _index_of(items: list[tuple[str, str]], key: str, default: int = 0) -> int:
    for i, (value, _label) in enumerate(items):
        if value == key:
            return i
    return default


class ChannelPanel(QGroupBox):
    """Control panel for a single speed channel ("pump" or "fan")."""

    def __init__(
        self,
        channel: str,
        title: str,
        cfg: ChannelConfig,
        on_apply,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.channel = channel
        self._cfg = cfg
        self._on_apply = on_apply

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)

        # --- profile + source selectors --------------------------------------
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.profile_combo = QComboBox()
        for _value, label in _PROFILES:
            self.profile_combo.addItem(label)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        form.addRow("Profile", self.profile_combo)

        self.source_combo = QComboBox()
        for _value, label in _SOURCES:
            self.source_combo.addItem(label)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        form.addRow("Curve input", self.source_combo)
        layout.addLayout(form)

        # --- stacked curve / fixed editors -----------------------------------
        self._stack = QStackedWidget()

        # Curve editor page.
        self.editor = CurveEditor(x_min=20, x_max=60, y_min=0, y_max=100)
        if channel == "pump":
            self.editor.set_y_floor(20)
        self.editor.curveChanged.connect(self._on_curve_changed)
        self._stack.addWidget(self.editor)

        # Fixed-duty page.
        fixed_page = QWidget()
        fixed_layout = QVBoxLayout(fixed_page)
        fixed_layout.setContentsMargins(0, 0, 0, 0)
        fixed_row = QHBoxLayout()
        self.fixed_slider = QSlider(Qt.Orientation.Horizontal)
        floor = 20 if channel == "pump" else 0
        self.fixed_slider.setRange(floor, 100)
        self.fixed_slider.setValue(max(floor, cfg.fixed_duty))
        self.fixed_spin = QSpinBox()
        self.fixed_spin.setRange(floor, 100)
        self.fixed_spin.setSuffix(" %")
        self.fixed_spin.setValue(max(floor, cfg.fixed_duty))
        self.fixed_slider.valueChanged.connect(self.fixed_spin.setValue)
        self.fixed_spin.valueChanged.connect(self.fixed_slider.setValue)
        fixed_row.addWidget(QLabel("Duty"))
        fixed_row.addWidget(self.fixed_slider, stretch=1)
        fixed_row.addWidget(self.fixed_spin)
        fixed_layout.addLayout(fixed_row)
        fixed_layout.addStretch(1)
        self._stack.addWidget(fixed_page)

        layout.addWidget(self._stack, stretch=1)

        # --- apply controls ---------------------------------------------------
        controls = QHBoxLayout()
        self.auto_apply = QCheckBox("Auto-apply on change")
        self.auto_apply.setChecked(False)
        controls.addWidget(self.auto_apply)
        controls.addStretch(1)
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._apply)
        controls.addWidget(self.apply_button)
        layout.addLayout(controls)

        self.load_config(cfg)

    # ------------------------------------------------------------------ config
    def load_config(self, cfg: ChannelConfig) -> None:
        """Populate every widget from ``cfg`` without emitting change signals."""
        self._cfg = cfg
        widgets = (self.profile_combo, self.source_combo, self.editor)
        blocked = [(w, w.blockSignals(True)) for w in widgets]
        try:
            self.profile_combo.setCurrentIndex(_index_of(_PROFILES, cfg.profile, 1))
            self.source_combo.setCurrentIndex(_index_of(_SOURCES, cfg.source, 0))

            points = cfg.points
            if cfg.profile in curves.PROFILES:
                points = curves.PROFILES[cfg.profile].get(self.channel, cfg.points)
            self.editor.set_curve(list(points))

            floor = 20 if self.channel == "pump" else 0
            self.fixed_slider.blockSignals(True)
            self.fixed_spin.blockSignals(True)
            self.fixed_slider.setValue(max(floor, cfg.fixed_duty))
            self.fixed_spin.setValue(max(floor, cfg.fixed_duty))
            self.fixed_slider.blockSignals(False)
            self.fixed_spin.blockSignals(False)
        finally:
            for widget, prev in blocked:
                widget.blockSignals(prev)
        self._sync_mode_widgets()

    def _sync_mode_widgets(self) -> None:
        """Show curve or fixed editor and enable/disable the source combo."""
        is_fixed = self._current_profile() == "fixed"
        self._stack.setCurrentIndex(1 if is_fixed else 0)
        self.source_combo.setEnabled(not is_fixed)
        self.editor.set_enabled_visual(not is_fixed)

    def _current_profile(self) -> str:
        idx = self.profile_combo.currentIndex()
        if 0 <= idx < len(_PROFILES):
            return _PROFILES[idx][0]
        return "custom"

    def _current_source(self) -> str:
        idx = self.source_combo.currentIndex()
        if 0 <= idx < len(_SOURCES):
            return _SOURCES[idx][0]
        return "liquid"

    # ------------------------------------------------------------------ slots
    def _on_profile_changed(self, _index: int) -> None:
        profile = self._current_profile()
        if profile in curves.PROFILES:
            # Load preset points into the editor without re-triggering Custom.
            points = curves.PROFILES[profile].get(self.channel, [])
            blocked = self.editor.blockSignals(True)
            try:
                self.editor.set_curve(list(points))
            finally:
                self.editor.blockSignals(blocked)
        self._sync_mode_widgets()
        if self.auto_apply.isChecked():
            self._apply()

    def _on_source_changed(self, _index: int) -> None:
        if self.auto_apply.isChecked():
            self._apply()

    def _on_curve_changed(self, _points: list) -> None:
        # A manual edit means the curve no longer matches any preset: flip to
        # Custom while blocking signals so we do not reload preset points.
        if self._current_profile() in curves.PROFILES:
            blocked = self.profile_combo.blockSignals(True)
            try:
                self.profile_combo.setCurrentIndex(_index_of(_PROFILES, "custom", 4))
            finally:
                self.profile_combo.blockSignals(blocked)
            self._sync_mode_widgets()
        if self.auto_apply.isChecked():
            self._apply()

    # ------------------------------------------------------------------ apply
    def build_config(self) -> ChannelConfig:
        """Collect the current widget state into a :class:`ChannelConfig`."""
        profile = self._current_profile()
        if profile == "fixed":
            mode = "fixed"
        else:
            mode = "curve"
        return ChannelConfig(
            mode=mode,
            source=self._current_source(),
            fixed_duty=int(self.fixed_spin.value()),
            points=[(float(t), int(d)) for t, d in self.editor.curve()],
            profile=profile,
        )

    def set_live_point(self, x: float | None, y: float | None) -> None:
        """Forward the live operating point to the curve editor marker."""
        try:
            self.editor.set_live_marker(x, y)
        except Exception:  # pragma: no cover - defensive against stub editors
            _LOGGER.debug("set_live_marker failed", exc_info=True)

    def _apply(self) -> None:
        cfg = self.build_config()
        self._cfg = cfg
        self._on_apply(self.channel, cfg)


class CoolingPage(QWidget):
    """Page hosting the pump and fan control panels."""

    def __init__(
        self,
        engine: "ControlEngine",
        config: "AppConfig",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        panels = QHBoxLayout()
        panels.setSpacing(14)
        self._pump_panel = ChannelPanel(
            "pump", "Pump", config.pump, self._apply_channel
        )
        self._fan_panel = ChannelPanel("fan", "Fan", config.fan, self._apply_channel)
        panels.addWidget(self._pump_panel, stretch=1)
        panels.addWidget(self._fan_panel, stretch=1)
        root.addLayout(panels, stretch=1)

        footer = QLabel(_FOOTER_NOTE)
        footer.setWordWrap(True)
        footer.setProperty("hint", True)
        root.addWidget(footer)

        try:
            self._engine.sample_ready.connect(self._on_sample)
        except Exception:  # pragma: no cover - defensive against stub engines
            _LOGGER.exception("could not connect sample_ready signal")

    def reload_from_config(self) -> None:
        """Reload both panels from the shared config.

        Called when something *outside* this page changes the config (e.g. the
        tray quick-profile apply), so the displayed curve/profile stays in sync
        with the engine's actual applied state instead of diverging until the app
        restarts.
        """
        self._pump_panel.load_config(self._config.pump)
        self._fan_panel.load_config(self._config.fan)

    # ------------------------------------------------------------------ apply
    def _apply_channel(self, channel: str, cfg: ChannelConfig) -> None:
        try:
            self._engine.apply_channel(channel, cfg)
        except Exception:
            _LOGGER.exception("apply_channel(%s) failed", channel)
            return

        # Update the shared config object and persist.
        if channel == "pump":
            self._config.pump = cfg
        elif channel == "fan":
            self._config.fan = cfg
        try:
            self._config.save()
        except Exception:
            _LOGGER.exception("config.save() failed after applying %s", channel)

    # ------------------------------------------------------------------ slots
    def _on_sample(self, status: "DeviceStatus", snap: "SystemSnapshot") -> None:
        liquid = getattr(status, "liquid_temp", None) if status is not None else None
        cpu = getattr(snap, "cpu_temp", None) if snap is not None else None
        gpu = getattr(snap, "gpu_temp", None) if snap is not None else None
        pump_duty = getattr(status, "pump_duty", None) if status is not None else None
        fan_duty = getattr(status, "fan_duty", None) if status is not None else None

        self._update_panel_marker(self._pump_panel, liquid, cpu, gpu, pump_duty)
        self._update_panel_marker(self._fan_panel, liquid, cpu, gpu, fan_duty)

    def _update_panel_marker(
        self,
        panel: ChannelPanel,
        liquid: float | None,
        cpu: float | None,
        gpu: float | None,
        duty: int | None,
    ) -> None:
        source = panel._current_source()
        if source == "cpu":
            x = cpu
        elif source == "gpu":
            x = gpu
        else:
            x = liquid
        panel.set_live_point(
            float(x) if x is not None else None,
            float(duty) if duty is not None else None,
        )
