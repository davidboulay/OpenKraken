"""LCD page: configure the round 640x640 display.

Left column selects the LCD mode (Liquid temp / Sensor screen / Static image /
Animated GIF / Screen off) with per-mode sub-options. The right column shows a
round, circularly-clipped preview. For sensor mode the preview renders
:func:`openkraken.backend.lcd_render.render` with the latest sample data via a 2 s
:class:`~PyQt6.QtCore.QTimer` (only while the page is visible). For static/GIF the
chosen file's first frame is shown circularly cropped; liquid/off get a drawn
placeholder.

*Apply* builds a :class:`~openkraken.config.LcdConfig`, calls ``engine.apply_lcd``
and persists via ``config.save()``.
"""

from __future__ import annotations

import dataclasses
import io
import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from openkraken.backend import lcd_render
from openkraken.config import LcdConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openkraken.backend.device import DeviceStatus
    from openkraken.backend.engine import ControlEngine
    from openkraken.backend.sensors import SystemSnapshot
    from openkraken.config import AppConfig

_LOGGER = logging.getLogger(__name__)

# Mode radio entries: stored value -> human label.
_MODES: list[tuple[str, str]] = [
    ("liquid", "Liquid temp (firmware)"),
    ("sensors", "Sensor screen (rendered)"),
    ("static", "Static image"),
    ("gif", "Animated GIF"),
    ("off", "Screen off"),
]

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
_GIF_FILTER = "Animated GIF (*.gif)"

_PREVIEW_PX = 320
_PREVIEW_REFRESH_MS = 2000

# Orientation combo entries: degrees.
_ORIENTATIONS = [0, 90, 180, 270]


class RoundPreview(QLabel):
    """A QLabel that displays a pixmap clipped to an inscribed circle."""

    def __init__(self, size_px: int = _PREVIEW_PX, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size_px = size_px
        self.setFixedSize(QSize(size_px, size_px))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._source: QPixmap | None = None
        self._placeholder_text = "LCD"

    def sizeHint(self) -> QSize:  # noqa: D102 - trivial
        return QSize(self._size_px, self._size_px)

    def set_image(self, pixmap: QPixmap | None) -> None:
        self._source = pixmap
        self.update()

    def set_placeholder(self, text: str) -> None:
        self._source = None
        self._placeholder_text = text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        path = QPainterPath()
        path.addEllipse(rect.adjusted(1, 1, -1, -1).toRectF())
        painter.setClipPath(path)

        # Backdrop matching the device's near-black face.
        painter.fillRect(rect, QColor("#0d0e12"))

        if self._source is not None and not self._source.isNull():
            scaled = self._source.scaled(
                self._size_px,
                self._size_px,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self._size_px - scaled.width()) // 2
            y = (self._size_px - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor("#8b8e98"))
            font = QFont()
            font.setPointSize(16)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._placeholder_text)

        # Subtle purple rim.
        painter.setClipping(False)
        pen = painter.pen()
        pen.setColor(QColor("#2a2d39"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        painter.end()


def _pil_to_qpixmap(image) -> QPixmap | None:
    """Convert a PIL image to a QPixmap via an in-memory PNG."""
    try:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        qimg = QImage.fromData(buf.getvalue(), "PNG")
        if qimg.isNull():
            return None
        return QPixmap.fromImage(qimg)
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("PIL -> QPixmap conversion failed", exc_info=True)
        return None


class LcdPage(QWidget):
    """Page for configuring the round LCD."""

    def __init__(
        self,
        engine: "ControlEngine",
        config: "AppConfig",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config
        # Work on a private copy, not the shared config.lcd the engine reads on its
        # own thread: _pick_file mutates image_path/gif_path before Apply, and the
        # engine snapshots through apply_lcd, so the only field changes that cross
        # the thread boundary go via that snapshot rather than a live shared object.
        self._lcd_cfg = dataclasses.replace(config.lcd)

        # Latest sample data, kept for the sensor-preview render.
        self._last_status: "DeviceStatus | None" = None
        self._last_snap: "SystemSnapshot | None" = None

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(18)

        root.addWidget(self._build_left(), stretch=1)
        root.addWidget(self._build_right())

        # Preview refresh timer: only renders sensor mode while the page shows.
        self._timer = QTimer(self)
        self._timer.setInterval(_PREVIEW_REFRESH_MS)
        self._timer.timeout.connect(self._refresh_preview)

        try:
            self._engine.sample_ready.connect(self._on_sample)
        except Exception:  # pragma: no cover - defensive against stub engines
            _LOGGER.exception("could not connect sample_ready signal")

        self._load_config(self._lcd_cfg)

    # ------------------------------------------------------------------ build
    def _build_left(self) -> QWidget:
        box = QGroupBox("Display mode")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)

        self._mode_group = QButtonGroup(self)
        self._mode_buttons: dict[str, QRadioButton] = {}
        for value, label in _MODES:
            rb = QRadioButton(label)
            self._mode_group.addButton(rb)
            self._mode_buttons[value] = rb
            layout.addWidget(rb)
        self._mode_group.buttonToggled.connect(self._on_mode_toggled)

        # --- sensor sub-options ----------------------------------------------
        self._sensor_box = QGroupBox("Sensor screen")
        sform = QFormLayout(self._sensor_box)
        self._style_combo = QComboBox()
        self._style_keys: list[str] = []
        for key, label in lcd_render.STYLES.items():
            self._style_keys.append(key)
            self._style_combo.addItem(label)
        self._style_combo.currentIndexChanged.connect(self._refresh_preview)
        sform.addRow("Style", self._style_combo)

        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.5, 10.0)
        self._interval_spin.setSingleStep(0.5)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setSuffix(" s")
        sform.addRow("Refresh", self._interval_spin)
        layout.addWidget(self._sensor_box)

        # --- static / gif sub-options ----------------------------------------
        self._file_box = QGroupBox("Media file")
        fbox = QVBoxLayout(self._file_box)
        pick_row = QHBoxLayout()
        self._pick_button = QPushButton("Choose file…")
        self._pick_button.clicked.connect(self._pick_file)
        pick_row.addWidget(self._pick_button)
        pick_row.addStretch(1)
        fbox.addLayout(pick_row)
        self._path_label = QLabel("(none)")
        self._path_label.setWordWrap(True)
        self._path_label.setProperty("hint", True)
        fbox.addWidget(self._path_label)
        layout.addWidget(self._file_box)

        layout.addStretch(1)

        # --- brightness + orientation ----------------------------------------
        common = QFormLayout()
        bright_row = QHBoxLayout()
        self._bright_slider = QSlider(Qt.Orientation.Horizontal)
        self._bright_slider.setRange(0, 100)
        self._bright_spin = QSpinBox()
        self._bright_spin.setRange(0, 100)
        self._bright_spin.setSuffix(" %")
        self._bright_slider.valueChanged.connect(self._bright_spin.setValue)
        self._bright_spin.valueChanged.connect(self._bright_slider.setValue)
        bright_row.addWidget(self._bright_slider, stretch=1)
        bright_row.addWidget(self._bright_spin)
        common.addRow("Brightness", bright_row)

        self._orient_combo = QComboBox()
        for deg in _ORIENTATIONS:
            self._orient_combo.addItem(f"{deg}°")
        common.addRow("Orientation", self._orient_combo)
        layout.addLayout(common)

        # --- apply + spec caption --------------------------------------------
        apply_row = QHBoxLayout()
        self._clear_media_button = QPushButton("Clear stored media")
        self._clear_media_button.setToolTip(
            "Erase images/GIFs stored in the cooler's onboard memory.\n"
            "The cooler replays the last uploaded media on its own during\n"
            "boot (before OpenKraken starts); clearing restores the firmware\n"
            "default boot screen. Your configured mode is re-applied after."
        )
        self._clear_media_button.clicked.connect(self._clear_stored_media)
        apply_row.addWidget(self._clear_media_button)
        apply_row.addStretch(1)
        self._apply_button = QPushButton("Apply")
        self._apply_button.clicked.connect(self._apply)
        apply_row.addWidget(self._apply_button)
        layout.addLayout(apply_row)

        caption = QLabel("640×640 · GIF ≤ 24 MB")
        caption.setProperty("hint", True)
        caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(caption)

        return box

    def _build_right(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Preview")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("hint", True)
        layout.addWidget(title)
        self._preview = RoundPreview(_PREVIEW_PX)
        layout.addWidget(self._preview, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        return wrap

    # ------------------------------------------------------------------ config
    def _load_config(self, cfg: LcdConfig) -> None:
        rb = self._mode_buttons.get(cfg.mode) or self._mode_buttons["liquid"]
        rb.blockSignals(True)
        rb.setChecked(True)
        rb.blockSignals(False)

        # Style combo.
        if cfg.sensor_style in self._style_keys:
            self._style_combo.setCurrentIndex(self._style_keys.index(cfg.sensor_style))

        self._interval_spin.setValue(
            min(10.0, max(0.5, float(cfg.sensor_interval)))
        )

        self._bright_slider.setValue(int(cfg.brightness))
        self._bright_spin.setValue(int(cfg.brightness))

        if cfg.orientation in _ORIENTATIONS:
            self._orient_combo.setCurrentIndex(_ORIENTATIONS.index(cfg.orientation))

        # File path label reflects whichever path matches the current mode.
        self._update_path_label()
        self._sync_mode_widgets()

    def _current_mode(self) -> str:
        for value, button in self._mode_buttons.items():
            if button.isChecked():
                return value
        return "liquid"

    def _current_style(self) -> str:
        idx = self._style_combo.currentIndex()
        if 0 <= idx < len(self._style_keys):
            return self._style_keys[idx]
        return next(iter(lcd_render.STYLES), "liquid_ring")

    def _sync_mode_widgets(self) -> None:
        mode = self._current_mode()
        self._sensor_box.setVisible(mode == "sensors")
        self._file_box.setVisible(mode in ("static", "gif"))
        self._update_path_label()
        self._refresh_preview()

    def _update_path_label(self) -> None:
        mode = self._current_mode()
        if mode == "static":
            path = self._lcd_cfg.image_path
        elif mode == "gif":
            path = self._lcd_cfg.gif_path
        else:
            path = ""
        self._path_label.setText(path or "(none)")

    # ------------------------------------------------------------------ slots
    def _on_mode_toggled(self, _button, checked: bool) -> None:
        if checked:
            self._sync_mode_widgets()

    def _pick_file(self) -> None:
        mode = self._current_mode()
        if mode == "gif":
            filt = _GIF_FILTER
            start = self._lcd_cfg.gif_path
        else:
            filt = _IMAGE_FILTER
            start = self._lcd_cfg.image_path
        path, _selected = QFileDialog.getOpenFileName(
            self, "Choose media file", start, filt
        )
        if not path:
            return
        if mode == "gif":
            self._lcd_cfg.gif_path = path
        else:
            self._lcd_cfg.image_path = path
        self._update_path_label()
        self._refresh_preview()

    def _on_sample(self, status: "DeviceStatus", snap: "SystemSnapshot") -> None:
        self._last_status = status
        self._last_snap = snap
        # Live preview is driven by the timer; no per-sample repaint here.

    # ------------------------------------------------------------------ preview
    def showEvent(self, event) -> None:  # noqa: D102 - Qt override
        super().showEvent(event)
        self._timer.start()
        self._refresh_preview()

    def hideEvent(self, event) -> None:  # noqa: D102 - Qt override
        super().hideEvent(event)
        self._timer.stop()

    def _refresh_preview(self) -> None:
        if not self.isVisible():
            return
        mode = self._current_mode()
        if mode == "sensors":
            self._render_sensor_preview()
        elif mode == "static":
            self._render_file_preview(self._lcd_cfg.image_path)
        elif mode == "gif":
            self._render_file_preview(self._lcd_cfg.gif_path)
        elif mode == "off":
            self._preview.set_placeholder("Screen off")
        else:  # liquid
            self._render_liquid_placeholder()

    def _render_sensor_preview(self) -> None:
        data = self._build_lcd_data()
        try:
            image = lcd_render.render(self._current_style(), data)
        except Exception:
            _LOGGER.exception("lcd_render.render failed")
            self._preview.set_placeholder("render error")
            return
        pixmap = _pil_to_qpixmap(image)
        if pixmap is None:
            self._preview.set_placeholder("render error")
        else:
            self._preview.set_image(pixmap)

    def _render_file_preview(self, path: str) -> None:
        if not path:
            self._preview.set_placeholder("No file selected")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            # Fall back to PIL (handles webp / animated gif first frame).
            try:
                from PIL import Image  # local import; Pillow is a backend dep

                with Image.open(path) as img:
                    img.seek(0)
                    pixmap = _pil_to_qpixmap(img) or QPixmap()
            except Exception:
                _LOGGER.debug("file preview load failed for %s", path, exc_info=True)
                pixmap = QPixmap()
        if pixmap.isNull():
            self._preview.set_placeholder("Cannot preview")
        else:
            self._preview.set_image(pixmap)

    def _render_liquid_placeholder(self) -> None:
        liquid = (
            getattr(self._last_status, "liquid_temp", None)
            if self._last_status is not None
            else None
        )
        if liquid is None:
            self._preview.set_placeholder("Liquid temp\n(firmware)")
        else:
            self._preview.set_placeholder(f"{liquid:.0f}°C\nLiquid (firmware)")

    def _build_lcd_data(self) -> "lcd_render.LcdData":
        status = self._last_status
        snap = self._last_snap
        return lcd_render.LcdData(
            liquid_temp=getattr(status, "liquid_temp", None) if status else None,
            cpu_temp=getattr(snap, "cpu_temp", None) if snap else None,
            cpu_load=getattr(snap, "cpu_load", None) if snap else None,
            gpu_temp=getattr(snap, "gpu_temp", None) if snap else None,
            gpu_load=getattr(snap, "gpu_load", None) if snap else None,
            pump_rpm=getattr(status, "pump_rpm", None) if status else None,
            fan_rpm=getattr(status, "fan_rpm", None) if status else None,
        )

    # ------------------------------------------------------------------ apply
    def _build_config(self) -> LcdConfig:
        orient_idx = self._orient_combo.currentIndex()
        orientation = _ORIENTATIONS[orient_idx] if 0 <= orient_idx < len(_ORIENTATIONS) else 0
        return LcdConfig(
            mode=self._current_mode(),
            brightness=int(self._bright_spin.value()),
            orientation=orientation,
            image_path=self._lcd_cfg.image_path,
            gif_path=self._lcd_cfg.gif_path,
            sensor_style=self._current_style(),
            sensor_interval=float(self._interval_spin.value()),
        )

    def _apply(self) -> None:
        cfg = self._build_config()
        self._lcd_cfg = cfg
        try:
            self._engine.apply_lcd(cfg)
        except Exception:
            _LOGGER.exception("apply_lcd failed")
            return
        self._config.lcd = cfg
        try:
            self._config.save()
        except Exception:
            _LOGGER.exception("config.save() failed after applying LCD config")

    def _clear_stored_media(self) -> None:
        """Ask the engine to erase the cooler's onboard media buckets."""
        try:
            self._engine.clear_lcd_media()
        except Exception:
            _LOGGER.exception("clear_lcd_media request failed")
