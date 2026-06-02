"""Lighting page: native LED control for the pump ring and RGB fan channel.

A top row toggles whether the app touches the LEDs at all ("Control LEDs") and
whether the ring config drives both channels ("Sync ring & fans"). Below are two
:class:`ChannelPanel` group boxes (Ring / Fans); the Fans panel is disabled and
dimmed while sync is on. Each panel offers a mode combo (from
:data:`krakencam.backend.lighting_fx.MODES`), a row of clickable colour swatches
(``QColorDialog``) bounded by the mode's ``min_colors``/``max_colors`` with +/−
buttons, a host-side brightness slider and a speed combo (shown only for animated
modes).

A round live preview approximates the 24-LED pump ring: 24 dots on a circle,
repainted once a second from :func:`krakencam.backend.lighting_fx.frame` so the
on-screen colours match what the device will show, animations included, at the
device's real ~1 FPS cadence (PROTOCOL.md §5). The preview timer only runs while
the page is visible.

*Apply* builds a :class:`~krakencam.config.LightingConfig`, calls
``engine.apply_lighting`` and persists via ``config.save()``. No byte-level
protocol lives here — the page only speaks ``lighting_fx`` (host-side effect
maths) and the engine's request API.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from krakencam.backend import lighting_fx
from krakencam.config import LightingChannelConfig, LightingConfig
from krakencam.gui.theme import COLORS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from krakencam.backend.engine import ControlEngine
    from krakencam.config import AppConfig

_LOGGER = logging.getLogger(__name__)

# Speed combo entries: stored value -> human label. Keys must match
# lighting_fx.SPEED_PERIODS.
_SPEEDS: list[tuple[str, str]] = [
    ("slow", "Slow"),
    ("normal", "Normal"),
    ("fast", "Fast"),
]

# Preview geometry.
_PREVIEW_PX = 200
_PREVIEW_RING_LEDS = 24  # ring LED count assumed by the preview (PROTOCOL.md §2)
_PREVIEW_DOT_RADIUS = 7.0
_PREVIEW_TICK_MS = 1000  # 1 Hz, matching the device's ~1 FPS limit (PROTOCOL.md §5)

#: Opacity applied to sections made unusable by the enable/sync toggles.
_DIM_OPACITY = 0.35

# Swatch button visual size.
_SWATCH_PX = 28
_MAX_SWATCH_BUTTONS = 8  # ceiling across all modes (cycle: max 8)

_CAPTION = (
    "Protocol reverse-engineered by the community (liquidctl PR #882 / OpenRGB). "
    "Effects are streamed from this app at ~1 frame/s (device limit) and stop "
    "when the app closes; colours reset on AC power-cycle."
)


def _mode_entries() -> list[tuple[str, str]]:
    """Return ``(key, label)`` pairs for the mode combo, in MODES order."""
    return [(spec.key, spec.label) for spec in lighting_fx.MODES.values()]


def _index_of(items: list[tuple[str, str]], key: str, default: int = 0) -> int:
    for i, (value, _label) in enumerate(items):
        if value == key:
            return i
    return default


def _clamp_rgb(value: int) -> int:
    return max(0, min(255, int(value)))


def _normalize_color(color: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        r, g, b = color
    except (TypeError, ValueError):
        return (124, 58, 237)
    return (_clamp_rgb(r), _clamp_rgb(g), _clamp_rgb(b))


class RingPreview(QWidget):
    """Round preview: 24 dots on a circle, fed RGB triplets each tick."""

    def __init__(self, size_px: int = _PREVIEW_PX, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size_px = size_px
        self.setFixedSize(QSize(size_px, size_px))
        self._colors: list[tuple[int, int, int]] = [
            (0, 0, 0) for _ in range(_PREVIEW_RING_LEDS)
        ]

    def sizeHint(self) -> QSize:  # noqa: D102 - trivial
        return QSize(self._size_px, self._size_px)

    def set_colors(self, colors: list[tuple[int, int, int]]) -> None:
        """Set the per-LED colours (any length; resampled to the dot count)."""
        n = _PREVIEW_RING_LEDS
        if not colors:
            self._colors = [(0, 0, 0) for _ in range(n)]
        elif len(colors) == n:
            self._colors = [_normalize_color(c) for c in colors]
        else:
            # Resample onto the fixed dot count so the preview still renders when
            # the detected LED count differs from the 24-dot ring drawing.
            src = colors
            m = len(src)
            self._colors = [_normalize_color(src[(i * m) // n]) for i in range(n)]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()

        # Dark round face matching the cooler.
        path = QPainterPath()
        path.addEllipse(rect.adjusted(1, 1, -1, -1).toRectF())
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(COLORS["bg"]))
        painter.setClipping(False)

        cx = rect.center().x()
        cy = rect.center().y()
        # Lay the dots on a circle inset from the rim so glow does not clip.
        ring_radius = (self._size_px / 2.0) - _PREVIEW_DOT_RADIUS - 10.0

        n = len(self._colors)
        for i, color in enumerate(self._colors):
            # Start at 12 o'clock, go clockwise (matches a physical pump ring).
            angle = -math.pi / 2.0 + (2.0 * math.pi * i / n)
            x = cx + ring_radius * math.cos(angle)
            y = cy + ring_radius * math.sin(angle)
            r, g, b = color
            dot = QColor(r, g, b)
            painter.setPen(Qt.PenStyle.NoPen)
            if r == 0 and g == 0 and b == 0:
                # An "off" LED reads as a dim socket, not a void.
                painter.setBrush(QColor(COLORS["panel2"]))
            else:
                # Soft outer glow.
                glow = QColor(r, g, b)
                glow.setAlpha(70)
                painter.setBrush(glow)
                painter.drawEllipse(
                    int(x - _PREVIEW_DOT_RADIUS - 3),
                    int(y - _PREVIEW_DOT_RADIUS - 3),
                    int((_PREVIEW_DOT_RADIUS + 3) * 2),
                    int((_PREVIEW_DOT_RADIUS + 3) * 2),
                )
                painter.setBrush(dot)
            painter.drawEllipse(
                int(x - _PREVIEW_DOT_RADIUS),
                int(y - _PREVIEW_DOT_RADIUS),
                int(_PREVIEW_DOT_RADIUS * 2),
                int(_PREVIEW_DOT_RADIUS * 2),
            )

        # Subtle rim to frame the ring.
        pen = QPen(QColor(COLORS["border"]))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        painter.end()


class SwatchButton(QPushButton):
    """A small flat button whose face shows a single RGB colour."""

    def __init__(self, color: tuple[int, int, int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = _normalize_color(color)
        self.setFixedSize(QSize(_SWATCH_PX, _SWATCH_PX))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_style()

    def color(self) -> tuple[int, int, int]:
        return self._color

    def set_color(self, color: tuple[int, int, int]) -> None:
        self._color = _normalize_color(color)
        self._refresh_style()

    def _refresh_style(self) -> None:
        r, g, b = self._color
        # Inline stylesheet keeps the swatch face exactly the chosen colour while
        # still picking up an accent border on hover, matching the app's buttons.
        self.setStyleSheet(
            "QPushButton {"
            f"background-color: rgb({r},{g},{b});"
            f"border: 1px solid {COLORS['border']};"
            "border-radius: 6px;"
            "}"
            "QPushButton:hover {"
            f"border: 2px solid {COLORS['accent']};"
            "}"
        )


class ChannelPanel(QGroupBox):
    """Control panel for a single lighting channel ("ring" or "fans")."""

    def __init__(
        self,
        channel: str,
        title: str,
        cfg: LightingChannelConfig,
        on_change,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.channel = channel
        # Work on a private copy; the page snapshots through apply.
        self._cfg = _copy_channel(cfg)
        self._on_change = on_change

        self._mode_entries = _mode_entries()
        self._swatch_buttons: list[SwatchButton] = []
        # Authoritative ordered colour list, independent of how many swatch
        # buttons are currently shown.
        self._colors: list[tuple[int, int, int]] = [
            _normalize_color(c) for c in cfg.colors
        ] or [(124, 58, 237)]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.mode_combo = QComboBox()
        for _key, label in self._mode_entries:
            self.mode_combo.addItem(label)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode", self.mode_combo)
        layout.addLayout(form)

        # --- colour swatches --------------------------------------------------
        self._colors_label = QLabel("Colours")
        self._colors_label.setProperty("hint", True)
        layout.addWidget(self._colors_label)

        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(6)
        self._swatch_container = QWidget()
        self._swatch_layout = QHBoxLayout(self._swatch_container)
        self._swatch_layout.setContentsMargins(0, 0, 0, 0)
        self._swatch_layout.setSpacing(6)
        swatch_row.addWidget(self._swatch_container)

        self._remove_button = _make_step_button("−", "Remove last colour")
        self._remove_button.clicked.connect(self._remove_color)
        self._add_button = _make_step_button("+", "Add a colour")
        self._add_button.clicked.connect(self._add_color)
        swatch_row.addWidget(self._remove_button)
        swatch_row.addWidget(self._add_button)
        swatch_row.addStretch(1)
        layout.addLayout(swatch_row)

        # --- brightness -------------------------------------------------------
        bright_form = QFormLayout()
        bright_form.setHorizontalSpacing(12)
        bright_row = QHBoxLayout()
        self.bright_slider = QSlider(Qt.Orientation.Horizontal)
        self.bright_slider.setRange(0, 100)
        self.bright_spin = QSpinBox()
        self.bright_spin.setRange(0, 100)
        self.bright_spin.setSuffix(" %")
        self.bright_slider.valueChanged.connect(self.bright_spin.setValue)
        self.bright_spin.valueChanged.connect(self.bright_slider.setValue)
        self.bright_slider.valueChanged.connect(lambda _v: self._notify())
        bright_row.addWidget(self.bright_slider, stretch=1)
        bright_row.addWidget(self.bright_spin)
        bright_form.addRow("Brightness", bright_row)

        # --- speed (animated modes only) -------------------------------------
        self.speed_combo = QComboBox()
        for _value, label in _SPEEDS:
            self.speed_combo.addItem(label)
        self.speed_combo.currentIndexChanged.connect(lambda _i: self._notify())
        self._speed_row_label = QLabel("Speed")
        bright_form.addRow(self._speed_row_label, self.speed_combo)
        layout.addLayout(bright_form)

        layout.addStretch(1)

        self.load_config(cfg)

    # ------------------------------------------------------------------ config
    def load_config(self, cfg: LightingChannelConfig) -> None:
        """Populate every widget from ``cfg`` without emitting change signals."""
        self._cfg = _copy_channel(cfg)
        self._colors = [_normalize_color(c) for c in cfg.colors] or [(124, 58, 237)]

        self.mode_combo.blockSignals(True)
        self.speed_combo.blockSignals(True)
        self.bright_slider.blockSignals(True)
        self.bright_spin.blockSignals(True)
        try:
            self.mode_combo.setCurrentIndex(
                _index_of(self._mode_entries, cfg.mode, 1)
            )
            self.bright_slider.setValue(_clamp_rgb_pct(cfg.brightness))
            self.bright_spin.setValue(_clamp_rgb_pct(cfg.brightness))
            self.speed_combo.setCurrentIndex(_index_of(_SPEEDS, cfg.speed, 1))
        finally:
            self.mode_combo.blockSignals(False)
            self.speed_combo.blockSignals(False)
            self.bright_slider.blockSignals(False)
            self.bright_spin.blockSignals(False)

        self._clamp_colors_to_mode()
        self._rebuild_swatches()
        self._sync_mode_widgets()

    def _spec(self):
        return lighting_fx.MODES[self._current_mode()]

    def _current_mode(self) -> str:
        idx = self.mode_combo.currentIndex()
        if 0 <= idx < len(self._mode_entries):
            return self._mode_entries[idx][0]
        return "fixed"

    def _current_speed(self) -> str:
        idx = self.speed_combo.currentIndex()
        if 0 <= idx < len(_SPEEDS):
            return _SPEEDS[idx][0]
        return "normal"

    def _clamp_colors_to_mode(self) -> None:
        """Trim/pad the colour list into the current mode's min/max range."""
        spec = self._spec()
        # Trim down to max (0 for swatch-less modes like off/spectrum).
        if len(self._colors) > spec.max_colors:
            self._colors = self._colors[: spec.max_colors]
        # Pad up to min.
        while len(self._colors) < spec.min_colors:
            self._colors.append(_default_extra_color(len(self._colors)))

    def _sync_mode_widgets(self) -> None:
        spec = self._spec()
        uses_colors = spec.max_colors > 0
        self._colors_label.setVisible(uses_colors)
        self._swatch_container.setVisible(uses_colors)
        self._add_button.setVisible(uses_colors)
        self._remove_button.setVisible(uses_colors)
        self._add_button.setEnabled(len(self._colors) < spec.max_colors)
        self._remove_button.setEnabled(len(self._colors) > spec.min_colors)

        animated = spec.animated
        self.speed_combo.setVisible(animated)
        self._speed_row_label.setVisible(animated)

    # ------------------------------------------------------------------ swatches
    def _rebuild_swatches(self) -> None:
        """Recreate swatch buttons to match ``self._colors``."""
        while self._swatch_buttons:
            btn = self._swatch_buttons.pop()
            self._swatch_layout.removeWidget(btn)
            btn.deleteLater()
        for index, color in enumerate(self._colors):
            btn = SwatchButton(color)
            btn.clicked.connect(lambda _checked, i=index: self._edit_color(i))
            self._swatch_layout.addWidget(btn)
            self._swatch_buttons.append(btn)
        self._sync_mode_widgets()

    def _edit_color(self, index: int) -> None:
        if not (0 <= index < len(self._colors)):
            return
        r, g, b = self._colors[index]
        chosen = QColorDialog.getColor(
            QColor(r, g, b), self, "Choose colour"
        )
        if not chosen.isValid():
            return
        self._colors[index] = (chosen.red(), chosen.green(), chosen.blue())
        if index < len(self._swatch_buttons):
            self._swatch_buttons[index].set_color(self._colors[index])
        self._notify()

    def _add_color(self) -> None:
        spec = self._spec()
        if len(self._colors) >= spec.max_colors:
            return
        self._colors.append(_default_extra_color(len(self._colors)))
        self._rebuild_swatches()
        self._notify()

    def _remove_color(self) -> None:
        spec = self._spec()
        if len(self._colors) <= spec.min_colors:
            return
        self._colors.pop()
        self._rebuild_swatches()
        self._notify()

    # ------------------------------------------------------------------ slots
    def _on_mode_changed(self, _index: int) -> None:
        self._clamp_colors_to_mode()
        self._rebuild_swatches()
        self._sync_mode_widgets()
        self._notify()

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("channel change callback failed", exc_info=True)

    # ------------------------------------------------------------------ build
    def build_config(self) -> LightingChannelConfig:
        """Collect the current widget state into a :class:`LightingChannelConfig`."""
        spec = self._spec()
        colors = [tuple(int(v) for v in c) for c in self._colors]
        # Persist colours bounded by the mode (swatch-less modes keep an empty
        # list so they round-trip cleanly).
        colors = colors[: spec.max_colors]
        return LightingChannelConfig(
            mode=self._current_mode(),
            colors=colors,
            brightness=int(self.bright_spin.value()),
            speed=self._current_speed(),
        )


class LightingPage(QWidget):
    """Page hosting the ring and fan lighting panels plus a live ring preview."""

    def __init__(
        self,
        engine: "ControlEngine",
        config: "AppConfig",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config
        # Work on a private copy of the lighting config; the engine snapshots
        # through apply_lighting so live edits never race the engine thread.
        self._lighting_cfg = _copy_lighting(config.lighting)

        # Monotonic origin for animated-preview time; reset whenever the preview
        # config changes so animations restart cleanly.
        self._preview_origin = time.monotonic()

        # Cached per-widget opacity effects used by _dim() (created lazily).
        self._dim_effects: dict[QWidget, QGraphicsOpacityEffect] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # --- top toggles ------------------------------------------------------
        top = QHBoxLayout()
        self._enable_check = QCheckBox("Control LEDs")
        self._enable_check.toggled.connect(self._on_enable_toggled)
        top.addWidget(self._enable_check)
        top.addStretch(1)
        self._detected_label = QLabel("")
        self._detected_label.setProperty("hint", True)
        top.addWidget(self._detected_label)
        root.addLayout(top)

        # --- panels + preview -------------------------------------------------
        body = QHBoxLayout()
        body.setSpacing(14)

        panels = QVBoxLayout()
        panels.setSpacing(12)
        self._ring_panel = ChannelPanel(
            "ring", "Ring", self._lighting_cfg.ring, self._on_panel_changed
        )
        self._fans_panel = ChannelPanel(
            "fans", "Fans", self._lighting_cfg.fans, self._on_panel_changed
        )
        # Sync sits directly above the Fans panel it overrides: when checked,
        # the ring config drives both channels and the fans panel dims.
        self._sync_check = QCheckBox("Sync ring && fans")
        self._sync_check.setToolTip(
            "Drive the fan LEDs with the Ring settings. The Fans panel below "
            "is ignored while this is on."
        )
        self._sync_check.toggled.connect(self._on_sync_toggled)
        panels.addWidget(self._ring_panel)
        panels.addSpacing(2)
        panels.addWidget(self._sync_check)
        panels.addWidget(self._fans_panel)
        panels.addStretch(1)
        body.addLayout(panels, stretch=1)

        # Preview column.
        preview_col = QWidget()
        self._preview_col = preview_col
        preview_layout = QVBoxLayout(preview_col)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        preview_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        ptitle = QLabel("Ring preview")
        ptitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ptitle.setProperty("hint", True)
        preview_layout.addWidget(ptitle)
        self._preview = RingPreview(_PREVIEW_PX)
        preview_layout.addWidget(
            self._preview, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        preview_layout.addStretch(1)
        body.addWidget(preview_col)

        root.addLayout(body, stretch=1)

        # --- apply + caption --------------------------------------------------
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {COLORS['border']};")
        root.addWidget(sep)

        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self._apply_button = QPushButton("Apply")
        self._apply_button.clicked.connect(self._apply)
        apply_row.addWidget(self._apply_button)
        root.addLayout(apply_row)

        caption = QLabel(_CAPTION)
        caption.setWordWrap(True)
        caption.setProperty("hint", True)
        root.addWidget(caption)

        # --- preview timer ----------------------------------------------------
        self._timer = QTimer(self)
        self._timer.setInterval(_PREVIEW_TICK_MS)
        self._timer.timeout.connect(self._refresh_preview)

        self._load_config(self._lighting_cfg)
        self._refresh_detected()

    # ------------------------------------------------------------------ config
    def _load_config(self, cfg: LightingConfig) -> None:
        self._enable_check.blockSignals(True)
        self._sync_check.blockSignals(True)
        try:
            self._enable_check.setChecked(bool(cfg.enabled))
            self._sync_check.setChecked(bool(cfg.sync))
        finally:
            self._enable_check.blockSignals(False)
            self._sync_check.blockSignals(False)
        self._ring_panel.load_config(cfg.ring)
        self._fans_panel.load_config(cfg.fans)
        self._sync_enabled_state()
        self._reset_preview_origin()

    def reload_from_config(self) -> None:
        """Reload every widget from the shared config (external mutation)."""
        self._lighting_cfg = _copy_lighting(self._config.lighting)
        self._load_config(self._lighting_cfg)

    def _dim(self, widget: QWidget, dimmed: bool) -> None:
        """Visually dim (and disable) a section that is currently not usable.

        ``setEnabled`` alone is too subtle in the dark theme, so a cached
        :class:`QGraphicsOpacityEffect` darkens the whole section; the effect is
        toggled rather than recreated (disabled effects are bypassed entirely
        when painting).
        """
        effect = self._dim_effects.get(widget)
        if effect is None:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(_DIM_OPACITY)
            effect.setEnabled(False)
            widget.setGraphicsEffect(effect)
            self._dim_effects[widget] = effect
        effect.setEnabled(dimmed)
        widget.setEnabled(not dimmed)

    def _sync_enabled_state(self) -> None:
        """Dim every section that the enable + sync toggles make unusable."""
        enabled = self._enable_check.isChecked()
        sync = self._sync_check.isChecked()
        # Master toggle off -> everything below it reads as inert.
        self._dim(self._ring_panel, not enabled)
        self._dim(self._sync_check, not enabled)
        self._dim(self._preview_col, not enabled)
        # Fans follow the ring while sync is on, so the panel is unusable.
        self._dim(self._fans_panel, (not enabled) or sync)

    def _refresh_detected(self) -> None:
        """Show detected per-channel LED counts from the device, if known."""
        counts = self._detected_led_counts()
        ring = counts.get("ring")
        fans = counts.get("fans")
        if ring is None and fans is None:
            self._detected_label.setText("")
            return
        parts = []
        if ring is not None:
            parts.append(f"Ring: {ring} LEDs")
        if fans is not None:
            parts.append(f"Fans: {fans} LEDs")
        self._detected_label.setText(" · ".join(parts) + " (detected)")

    def _detected_led_counts(self) -> dict[str, int]:
        """Read ``device.lighting_info.led_counts`` defensively (may be absent)."""
        device = getattr(self._engine, "device", None)
        info = getattr(device, "lighting_info", None) if device is not None else None
        counts = getattr(info, "led_counts", None) if info is not None else None
        if isinstance(counts, dict):
            out: dict[str, int] = {}
            for key in ("ring", "fans"):
                value = counts.get(key)
                if isinstance(value, int):
                    out[key] = value
            return out
        return {}

    def _led_count_for(self, channel: str, default: int) -> int:
        counts = self._detected_led_counts()
        value = counts.get(channel)
        if isinstance(value, int) and value > 0:
            return value
        return default

    # ------------------------------------------------------------------ slots
    def _on_enable_toggled(self, _checked: bool) -> None:
        self._sync_enabled_state()
        self._refresh_preview()

    def _on_sync_toggled(self, _checked: bool) -> None:
        self._sync_enabled_state()
        self._refresh_preview()

    def _on_panel_changed(self) -> None:
        # Any swatch / mode / brightness / speed edit restarts the animation
        # clock so the preview reflects the new config from t=0.
        self._reset_preview_origin()
        self._refresh_preview()

    def _reset_preview_origin(self) -> None:
        self._preview_origin = time.monotonic()

    # ------------------------------------------------------------------ preview
    def showEvent(self, event) -> None:  # noqa: D102 - Qt override
        super().showEvent(event)
        self._reset_preview_origin()
        self._timer.start()
        self._refresh_preview()

    def hideEvent(self, event) -> None:  # noqa: D102 - Qt override
        super().hideEvent(event)
        self._timer.stop()

    def _refresh_preview(self) -> None:
        if not self.isVisible():
            return
        # The preview always shows the RING channel (the pump ring is what the
        # round widget depicts). When sync is on this is also what the fans get.
        panel = self._ring_panel
        cfg = panel.build_config()
        led_count = self._led_count_for("ring", _PREVIEW_RING_LEDS)
        t = time.monotonic() - self._preview_origin
        try:
            # Pass the channel's speed so the preview animates at the SAME cadence
            # the engine will drive on the device.  The engine time-warps elapsed
            # by normal_period/speed_period before calling frame() at the default
            # "normal" speed; feeding frame() the real elapsed plus speed=cfg.speed
            # yields the identical phase (t / speed_period), so the two cannot
            # drift (previously the preview always ran at "normal").
            colors = lighting_fx.frame(
                cfg.mode,
                [tuple(int(v) for v in c) for c in cfg.colors],
                int(cfg.brightness),
                led_count,
                t,
                speed=cfg.speed,
            )
        except Exception:
            _LOGGER.debug("lighting_fx.frame failed", exc_info=True)
            self._preview.set_colors([(0, 0, 0)] * led_count)
            return
        self._preview.set_colors([_normalize_color(c) for c in colors])

    # ------------------------------------------------------------------ apply
    def _build_config(self) -> LightingConfig:
        ring_cfg = self._ring_panel.build_config()
        if self._sync_check.isChecked():
            # Sync: the ring config drives both channels (engine applies ring to
            # both, but we persist a matching copy for the fans so the saved file
            # is self-consistent).
            fans_cfg = _copy_channel(ring_cfg)
        else:
            fans_cfg = self._fans_panel.build_config()
        return LightingConfig(
            enabled=bool(self._enable_check.isChecked()),
            sync=bool(self._sync_check.isChecked()),
            ring=ring_cfg,
            fans=fans_cfg,
        )

    def _apply(self) -> None:
        cfg = self._build_config()
        self._lighting_cfg = cfg
        try:
            self._engine.apply_lighting(cfg)
        except Exception:
            _LOGGER.exception("apply_lighting failed")
            return
        self._config.lighting = cfg
        try:
            self._config.save()
        except Exception:
            _LOGGER.exception("config.save() failed after applying lighting config")
        # Keep the (possibly synced) fans panel display consistent with what was
        # applied so it does not show stale colours when sync is later turned off.
        if cfg.sync:
            self._fans_panel.load_config(cfg.fans)


# --------------------------------------------------------------------------- #
# Module helpers                                                               #
# --------------------------------------------------------------------------- #
def _make_step_button(glyph: str, tooltip: str) -> QPushButton:
    """A compact square +/− button sized to match the colour swatches."""
    btn = QPushButton(glyph)
    btn.setFixedSize(QSize(_SWATCH_PX, _SWATCH_PX))
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    # Override the global QPushButton padding (7px 14px) so the glyph centres in
    # the small square instead of being clipped.
    btn.setStyleSheet(
        "QPushButton {"
        f"background-color: {COLORS['panel2']};"
        f"border: 1px solid {COLORS['border']};"
        "border-radius: 6px;"
        "padding: 0px;"
        "font-size: 18px;"
        "font-weight: 700;"
        "}"
        "QPushButton:hover {"
        f"background-color: {COLORS['accent_hover']};"
        f"border-color: {COLORS['accent_hover']};"
        "color: #ffffff;"
        "}"
        "QPushButton:disabled {"
        f"background-color: {COLORS['panel']};"
        f"color: {COLORS['text_dim']};"
        "}"
    )
    return btn


def _clamp_rgb_pct(value: int) -> int:
    return max(0, min(100, int(value)))


def _default_extra_color(index: int) -> tuple[int, int, int]:
    """Pick a pleasant default for a newly-added swatch.

    Cycles a small accent-friendly palette so successive *add* clicks do not all
    produce identical purple swatches.
    """
    palette = [
        (124, 58, 237),   # accent purple
        (56, 189, 248),   # cpu blue
        (52, 211, 153),   # gpu green
        (244, 114, 182),  # pump pink
        (251, 191, 36),   # fan amber
        (239, 68, 68),    # crit red
        (236, 237, 241),  # near-white
        (143, 85, 240),   # accent hover
    ]
    return palette[index % len(palette)]


def _copy_channel(cfg: LightingChannelConfig) -> LightingChannelConfig:
    """Deep-copy a channel config (the colours list must not be shared)."""
    return dataclasses.replace(
        cfg, colors=[tuple(int(v) for v in c) for c in cfg.colors]
    )


def _copy_lighting(cfg: LightingConfig) -> LightingConfig:
    """Deep-copy a lighting config including both channel sub-configs."""
    return dataclasses.replace(
        cfg,
        ring=_copy_channel(cfg.ring),
        fans=_copy_channel(cfg.fans),
    )
