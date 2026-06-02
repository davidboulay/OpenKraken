"""Settings page: application preferences, device info and about box.

A form of application options (poll interval, history window, startup behaviour),
a device box (model / firmware / connection state with a *Reconnect* button) and an
about box. *Save* persists the config and pushes the new options to the engine via
``engine.update_config``. *Reconnect* calls ``engine.request_reconnect``.

The connection state label reacts to the engine's ``connection_changed`` signal and
is resilient to a disconnected device at startup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from openkraken import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openkraken.backend.engine import ControlEngine
    from openkraken.config import AppConfig

_LOGGER = logging.getLogger(__name__)

_LIQUIDCTL_URL = "https://github.com/liquidctl/liquidctl"

_DISCLAIMER = (
    "OpenKraken is an unofficial, community tool and is not affiliated with NZXT. "
    "It controls cooling hardware directly — use at your own risk."
)


class SettingsPage(QWidget):
    """Application settings, device status and about information."""

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
        root.setSpacing(14)

        root.addWidget(self._build_app_box())
        root.addWidget(self._build_device_box())
        root.addWidget(self._build_about_box())
        root.addStretch(1)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self._save_button = QPushButton("Save")
        self._save_button.clicked.connect(self._save)
        save_row.addWidget(self._save_button)
        root.addLayout(save_row)

        self._load_config()
        self._refresh_device_info()

        # React to connection transitions emitted by the engine.
        try:
            self._engine.connection_changed.connect(self._on_connection_changed)
        except Exception:  # pragma: no cover - defensive against stub engines
            _LOGGER.exception("could not connect connection_changed signal")

    # ------------------------------------------------------------------ build
    def _build_app_box(self) -> QWidget:
        box = QGroupBox("Application")
        form = QFormLayout(box)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self._poll_spin = QDoubleSpinBox()
        self._poll_spin.setRange(0.5, 5.0)
        self._poll_spin.setSingleStep(0.5)
        self._poll_spin.setDecimals(1)
        self._poll_spin.setSuffix(" s")
        form.addRow("Poll interval", self._poll_spin)

        self._history_spin = QSpinBox()
        self._history_spin.setRange(60, 3600)
        self._history_spin.setSingleStep(60)
        self._history_spin.setSuffix(" s")
        form.addRow("History window", self._history_spin)

        self._start_min = QCheckBox("Start minimized")
        form.addRow("", self._start_min)

        self._close_tray = QCheckBox("Close to tray")
        form.addRow("", self._close_tray)

        self._run_background = QCheckBox(
            "Keep running in background when closed (no tray)"
        )
        self._run_background.setToolTip(
            "Without a system tray (e.g. stock GNOME), closing the window keeps "
            "cooling/lighting active. Reopen by launching OpenKraken again. "
            "Press Ctrl+Q to quit."
        )
        form.addRow("", self._run_background)

        self._apply_start = QCheckBox("Apply saved settings on start")
        form.addRow("", self._apply_start)

        return box

    def _build_device_box(self) -> QWidget:
        box = QGroupBox("Device")
        form = QFormLayout(box)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self._model_label = QLabel("—")
        form.addRow("Model", self._model_label)

        self._fw_label = QLabel("—")
        form.addRow("Firmware", self._fw_label)

        self._conn_label = QLabel("Disconnected")
        form.addRow("Connection", self._conn_label)

        reconnect_row = QHBoxLayout()
        self._reconnect_button = QPushButton("Reconnect")
        self._reconnect_button.clicked.connect(self._reconnect)
        reconnect_row.addWidget(self._reconnect_button)
        reconnect_row.addStretch(1)
        form.addRow("", reconnect_row)

        return box

    def _build_about_box(self) -> QWidget:
        box = QGroupBox("About")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        version = QLabel(f"OpenKraken v{__version__}")
        layout.addWidget(version)

        link = QLabel(
            f'Built on <a href="{_LIQUIDCTL_URL}">liquidctl</a> + PyQt6.'
        )
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(link)

        disclaimer = QLabel(_DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setProperty("hint", True)
        layout.addWidget(disclaimer)

        return box

    # ------------------------------------------------------------------ config
    def _load_config(self) -> None:
        cfg = self._config
        self._poll_spin.setValue(min(5.0, max(0.5, float(cfg.poll_interval))))
        self._history_spin.setValue(min(3600, max(60, int(cfg.history_seconds))))
        self._start_min.setChecked(bool(cfg.start_minimized))
        self._close_tray.setChecked(bool(cfg.close_to_tray))
        self._run_background.setChecked(bool(cfg.run_in_background))
        self._apply_start.setChecked(bool(cfg.apply_on_start))

    def _refresh_device_info(self) -> None:
        device = getattr(self._engine, "device", None)
        description = getattr(device, "description", "") if device is not None else ""
        connected = bool(getattr(device, "is_connected", False)) if device else False
        firmware = getattr(device, "firmware_version", "") if device else ""

        self._model_label.setText(description or "NZXT Kraken (not detected)")
        self._fw_label.setText(firmware or "—")
        self._set_connection(connected, description)

    def _set_connection(self, connected: bool, description: str) -> None:
        if connected:
            self._conn_label.setText("Connected")
            self._conn_label.setStyleSheet("color: #34d399;")
            if description:
                self._model_label.setText(description)
        else:
            self._conn_label.setText("Disconnected")
            self._conn_label.setStyleSheet("color: #ef4444;")

    # ------------------------------------------------------------------ slots
    def _on_connection_changed(self, connected: bool, description: str) -> None:
        self._set_connection(connected, description)
        if connected:
            device = getattr(self._engine, "device", None)
            firmware = getattr(device, "firmware_version", "") if device else ""
            if firmware:
                self._fw_label.setText(firmware)

    def _reconnect(self) -> None:
        try:
            self._engine.request_reconnect()
        except Exception:
            _LOGGER.exception("request_reconnect failed")

    def _save(self) -> None:
        cfg = self._config
        cfg.poll_interval = float(self._poll_spin.value())
        cfg.history_seconds = int(self._history_spin.value())
        cfg.start_minimized = bool(self._start_min.isChecked())
        cfg.close_to_tray = bool(self._close_tray.isChecked())
        cfg.run_in_background = bool(self._run_background.isChecked())
        cfg.apply_on_start = bool(self._apply_start.isChecked())

        try:
            cfg.save()
        except Exception:
            _LOGGER.exception("config.save() failed")
        try:
            self._engine.update_config(cfg)
        except Exception:
            _LOGGER.exception("engine.update_config failed")
