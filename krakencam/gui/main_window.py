"""Main application window for Kraken CAM.

Hosts the left navigation sidebar, the stacked pages (Dashboard / Cooling /
LCD / Settings), the status bar, and the optional system-tray integration
(profile quick-apply + show/hide/quit).

The window is purely a GUI-thread object: it never touches the device or
sensors directly. All hardware work goes through the :class:`ControlEngine`,
which it talks to via thread-safe request methods and listens to via signals.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from krakencam import __version__
from krakencam.backend import curves
from krakencam.backend.device import DeviceStatus
from krakencam.backend.engine import ControlEngine
from krakencam.backend.sensors import SystemSnapshot
from krakencam.config import AppConfig, ChannelConfig
from krakencam.gui import theme
from krakencam.gui.pages.cooling import CoolingPage
from krakencam.gui.pages.dashboard import DashboardPage
from krakencam.gui.pages.lcd import LcdPage
from krakencam.gui.pages.lighting import LightingPage
from krakencam.gui.pages.settings import SettingsPage

_LOGGER = logging.getLogger(__name__)

# Status-bar message lifetimes (milliseconds).
_APPLIED_MSG_MS = 5_000
_ERROR_MSG_MS = 8_000

_SIDEBAR_WIDTH = 200
_MIN_SIZE = (920, 640)

# Navigation entries: (label, page attribute name).
_NAV_ITEMS = (
    ("Dashboard", "_dashboard"),
    ("Cooling", "_cooling"),
    ("Lighting", "_lighting"),
    ("LCD", "_lcd"),
    ("Settings", "_settings"),
)

# Quick-apply profiles exposed in the tray menu (applied to BOTH channels).
_TRAY_PROFILES = ("silent", "balanced", "performance")


class MainWindow(QMainWindow):
    """Top-level application window.

    Parameters
    ----------
    engine:
        The running control engine. The window connects to its signals and
        issues quick-apply / lifecycle requests through it.
    config:
        The shared application config (mutated in-place by pages).
    """

    #: Set once (per process) the first time the window hides to background with
    #: no tray, so the explanatory log line is emitted only once.
    _logged_background_notice = False

    def __init__(self, engine: ControlEngine, config: AppConfig) -> None:
        super().__init__()
        self._engine = engine
        self._config = config

        # Tracks the last integer liquid temperature pushed to the tray icon so
        # we only re-render the icon when the displayed value would change.
        self._last_tray_temp: int | None = None
        # Set True only when the user explicitly quits, so closeEvent knows to
        # really exit instead of hiding to tray.
        self._really_quit = False

        self.setWindowTitle("Kraken CAM")
        self.setWindowIcon(theme.make_app_icon())
        self.setMinimumSize(*_MIN_SIZE)

        self._build_ui()
        self._build_tray()
        self._connect_signals()

        # Ctrl+Q always quits, even with no tray (where closing only hides the
        # window). Documented in the Settings "run in background" tooltip.
        self._quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        self._quit_shortcut.activated.connect(self._quit)

        # Start on the dashboard.
        self._nav_buttons[0].setChecked(True)
        self._stack.setCurrentIndex(0)

    # ------------------------------------------------------------------ UI ---
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_stack(), 1)

        self.setCentralWidget(central)

        # Status bar (created lazily by Qt on first access).
        self.statusBar().showMessage("Starting…")

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(_SIDEBAR_WIDTH)
        sidebar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 14)
        layout.setSpacing(6)

        # --- Title block -----------------------------------------------------
        title = QLabel("KRAKEN", sidebar)
        title.setObjectName("appTitle")
        subtitle = QLabel("CAM", sidebar)
        subtitle.setObjectName("appTitleAccent")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(18)

        # --- Navigation buttons ---------------------------------------------
        self._nav_group = QButtonGroup(sidebar)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []

        for index, (label, _attr) in enumerate(_NAV_ITEMS):
            button = QPushButton(label, sidebar)
            button.setCheckable(True)
            button.setProperty("sidebar", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, i=index: self._show_page(i))
            self._nav_group.addButton(button, index)
            self._nav_buttons.append(button)
            layout.addWidget(button)

        layout.addStretch(1)

        # --- Connection pill -------------------------------------------------
        self._conn_pill = QLabel("●  Disconnected", sidebar)
        self._conn_pill.setObjectName("connPill")
        self._conn_pill.setProperty("connected", False)
        self._conn_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._conn_pill)

        return sidebar

    def _build_stack(self) -> QWidget:
        self._stack = QStackedWidget(self)

        self._dashboard = DashboardPage(self._engine, self._config)
        self._cooling = CoolingPage(self._engine, self._config)
        self._lighting = LightingPage(self._engine, self._config)
        self._lcd = LcdPage(self._engine, self._config)
        self._settings = SettingsPage(self._engine, self._config)

        for _label, attr in _NAV_ITEMS:
            self._stack.addWidget(getattr(self, attr))

        return self._stack

    def _build_tray(self) -> None:
        """Create the system tray icon if a tray is available.

        On COSMIC/Wayland an SNI host may or may not be exposed; when it is
        unavailable the window simply runs without a tray and close-to-tray is
        treated as a plain close.
        """
        self._tray: QSystemTrayIcon | None = None

        if not QSystemTrayIcon.isSystemTrayAvailable():
            _LOGGER.info("System tray not available; running without tray icon.")
            return

        tray = QSystemTrayIcon(self)
        tray.setIcon(theme.make_tray_icon(None))
        tray.setToolTip("Kraken CAM")

        menu = QMenu()

        self._show_hide_action = QAction("Hide window", menu)
        self._show_hide_action.triggered.connect(self._toggle_visibility)
        menu.addAction(self._show_hide_action)

        menu.addSeparator()
        profile_menu = menu.addMenu("Apply profile")
        for profile in _TRAY_PROFILES:
            action = QAction(profile.capitalize(), profile_menu)
            action.triggered.connect(lambda _checked, p=profile: self._apply_profile(p))
            profile_menu.addAction(action)

        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()

        self._tray = tray
        # Match the initial label to the window's actual visibility. When launched
        # with --minimized (and a tray present) app.py skips show(), so no
        # showEvent fires; without this the menu would wrongly read "Hide window".
        self._update_show_hide_label()
        _LOGGER.debug("System tray icon initialized.")

    def _connect_signals(self) -> None:
        self._engine.sample_ready.connect(self._on_sample)
        self._engine.connection_changed.connect(self._on_connection_changed)
        self._engine.applied.connect(self._on_applied)
        self._engine.error.connect(self._on_error)

    # ------------------------------------------------------- navigation ---
    def _show_page(self, index: int) -> None:
        self._stack.setCurrentIndex(index)

    # ---------------------------------------------------- engine signals ---
    def _on_sample(self, status: object, _snap: object) -> None:
        """Refresh the tray icon when the integer liquid temperature changes."""
        if self._tray is None:
            return
        if not isinstance(status, DeviceStatus):
            return

        temp = status.liquid_temp
        # Use the SAME rounding the icon renderer uses (theme.make_tray_icon draws
        # ``int(round(temp))``); truncating with int() here would disagree on the
        # fractional half and under-report by 1 degree / skip a needed re-render.
        new_temp = int(round(temp)) if temp is not None else None
        if new_temp == self._last_tray_temp:
            return

        self._last_tray_temp = new_temp
        self._tray.setIcon(theme.make_tray_icon(temp))
        if temp is not None:
            self._tray.setToolTip(f"Kraken CAM — liquid {temp:.0f}°C")
        else:
            self._tray.setToolTip("Kraken CAM")

    def _on_connection_changed(self, connected: bool, description: str) -> None:
        if connected:
            label = description or "Kraken Elite"
            # Elide so a long model name (e.g. "NZXT Kraken 2024 Elite RGB")
            # never overflows the fixed-width sidebar pill.
            avail = max(60, self._conn_pill.width() - 24)
            metrics = QFontMetrics(self._conn_pill.font())
            elided = metrics.elidedText(label, Qt.TextElideMode.ElideRight, avail)
            self._conn_pill.setText(f"●  {elided}")
        else:
            self._conn_pill.setText("●  Disconnected")

        self._conn_pill.setProperty("connected", connected)
        # Re-polish so the [connected] QSS selector updates.
        self._conn_pill.style().unpolish(self._conn_pill)
        self._conn_pill.style().polish(self._conn_pill)

    def _on_applied(self, what: str, detail: str) -> None:
        message = f"Applied {what}: {detail}" if detail else f"Applied {what}"
        # An informational message must never inherit the error's red styling, so
        # clear it before showing (an error's 8 s timer may still be pending).
        self.statusBar().setStyleSheet("")
        self.statusBar().showMessage(message, _APPLIED_MSG_MS)

    def _on_error(self, message: str) -> None:
        bar = self.statusBar()
        # Style the bar red for the duration of the error message.
        bar.setStyleSheet(f"color: {theme.COLORS['crit']};")
        bar.showMessage(f"Error: {message}", _ERROR_MSG_MS)
        # Clear the red styling once the message expires.  Connect with a unique
        # connection so repeated errors do not stack duplicate slots.
        bar.messageChanged.connect(
            self._on_status_message_changed,
            Qt.ConnectionType.UniqueConnection,
        )

    def _on_status_message_changed(self, text: str) -> None:
        if not text:
            self.statusBar().setStyleSheet("")
            try:
                self.statusBar().messageChanged.disconnect(
                    self._on_status_message_changed
                )
            except TypeError:
                pass

    # ----------------------------------------------------------- tray ---
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _toggle_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._restore_window()

    def show_and_raise(self) -> None:
        """Show, raise and focus the window.

        Used by the single-instance activation path (a second launch asks the
        running instance to surface its window). On Wayland ``raise_()`` /
        ``activateWindow()`` are advisory requests to the compositor, which is
        fine — the window is at least shown.
        """
        self.show()
        self.raise_()
        self.activateWindow()
        self._update_show_hide_label()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _update_show_hide_label(self) -> None:
        if not hasattr(self, "_show_hide_action"):
            return
        visible = self.isVisible() and not self.isMinimized()
        self._show_hide_action.setText("Hide window" if visible else "Show window")

    def _apply_profile(self, profile: str) -> None:
        """Quick-apply a named profile to BOTH pump and fan channels.

        Builds a :class:`ChannelConfig` from ``curves.PROFILES`` for each
        channel while preserving that channel's current x-axis source (the tray
        applies firmware-native liquid curves, so source is forced to
        ``"liquid"`` to match the preset's liquid-keyed points).
        """
        preset = curves.PROFILES.get(profile)
        if preset is None:
            _LOGGER.warning("Unknown tray profile requested: %s", profile)
            return

        for channel, current in (("pump", self._config.pump), ("fan", self._config.fan)):
            points = [
                (float(temp), int(duty)) for temp, duty in preset.get(channel, [])
            ]
            cfg = ChannelConfig(
                mode="curve",
                source="liquid",
                fixed_duty=current.fixed_duty,
                points=points,
                profile=profile,
            )
            # Mutate the shared config so pages reflect the change on next open.
            if channel == "pump":
                self._config.pump = cfg
            else:
                self._config.fan = cfg
            self._engine.apply_channel(channel, cfg)

        try:
            self._config.save()
        except Exception:  # pragma: no cover - persistence is best-effort
            _LOGGER.exception("Failed to save config after tray profile apply.")

        # Keep the (already-built) Cooling page in sync with the new config so it
        # does not display the previously-edited curve until restart.
        try:
            self._cooling.reload_from_config()
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Failed to reload cooling page after tray profile.")

        _LOGGER.info("Applied profile %r to pump and fan via tray.", profile)

    # ------------------------------------------------------ lifecycle ---
    def _quit(self) -> None:
        """Stop the engine and quit the application from the tray."""
        self._really_quit = True
        _LOGGER.info("Quit requested from tray.")
        self._engine.stop()
        QApplication.quit()

    def _close_action(self) -> str:
        """Decide what closing the window should do.

        Returns one of:

        - ``"tray"`` — a tray exists and ``close_to_tray`` is set: hide to tray.
        - ``"background"`` — no tray but ``run_in_background`` is set: hide the
          window and keep the engine running (cooling/lighting/LCD stay active);
          re-launching Kraken CAM reopens it.
        - ``"quit"`` — stop the engine and exit the application.

        An explicit user quit (``self._really_quit``) always yields ``"quit"``.
        """
        if self._really_quit:
            return "quit"

        tray_active = self._tray is not None and self._tray.isVisible()
        if tray_active and self._config.close_to_tray:
            return "tray"
        if not tray_active and self._config.run_in_background:
            return "background"
        return "quit"

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt name)
        """Hide to tray / background when configured, else stop and exit."""
        action = self._close_action()

        if action == "tray":
            event.ignore()
            self.hide()
            self._update_show_hide_label()
            if self._tray is not None:
                self._tray.showMessage(
                    "Kraken CAM",
                    "Still running in the tray. Right-click to quit.",
                    theme.make_app_icon(),
                    3_000,
                )
            return

        if action == "background":
            event.ignore()
            self.hide()
            self._update_show_hide_label()
            if not MainWindow._logged_background_notice:
                MainWindow._logged_background_notice = True
                _LOGGER.info(
                    "running in background; launch Kraken CAM again to reopen "
                    "the window (Ctrl+Q quits)"
                )
            return

        _LOGGER.info("Window closing; stopping engine.")
        self._really_quit = True
        self._engine.stop()
        event.accept()
        # quitOnLastWindowClosed is False (so hide-to-tray/background works), so
        # closing the window does not by itself end the event loop. Quit
        # explicitly here or the process would keep spinning with no window.
        QApplication.quit()

    # ------------------------------------------------------------ Qt ---
    def hideEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().hideEvent(event)
        self._update_show_hide_label()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self._update_show_hide_label()
