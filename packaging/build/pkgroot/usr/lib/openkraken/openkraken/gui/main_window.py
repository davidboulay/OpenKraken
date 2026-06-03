"""Main application window for OpenKraken.

Hosts the left navigation sidebar, the stacked pages (Dashboard / Cooling /
LCD / Settings), the status bar, and the optional system-tray integration
(profile quick-apply + show/hide/quit).

The window is purely a GUI-thread object: it never touches the device or
sensors directly. All hardware work goes through the :class:`ControlEngine`,
which it talks to via thread-safe request methods and listens to via signals.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QTimer

try:  # QtDBus is Linux-only but always present in the PyQt6 builds we target.
    from PyQt6.QtDBus import QDBusConnection, QDBusServiceWatcher

    _HAS_QTDBUS = True
except ImportError:  # pragma: no cover - exotic builds without QtDBus
    _HAS_QTDBUS = False
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
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

from openkraken import __version__
from openkraken.backend import curves
from openkraken.backend.device import DeviceStatus
from openkraken.backend.engine import ControlEngine
from openkraken.backend.sensors import SystemSnapshot
from openkraken.config import AppConfig, ChannelConfig
from openkraken.gui import theme
from openkraken.gui.pages.cooling import CoolingPage
from openkraken.gui.pages.dashboard import DashboardPage
from openkraken.gui.pages.lcd import LcdPage
from openkraken.gui.pages.lighting import LightingPage
from openkraken.gui.pages.settings import SettingsPage

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

        self.setWindowTitle("OpenKraken")
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

        # --- Title block: app logo + wordmark ---------------------------------
        # Wordmark only -- the droplet already shows in the window titlebar /
        # taskbar (setWindowIcon), so a second one here just duplicated it.
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        wordmark = QLabel(sidebar)
        wordmark.setObjectName("appTitle")
        wordmark.setTextFormat(Qt.TextFormat.RichText)
        wordmark.setText(
            f'<span style="color:{theme.COLORS["text"]};">Open</span>'
            f'<span style="color:{theme.COLORS["accent"]};">Kraken</span>'
        )
        title_row.addWidget(wordmark)
        title_row.addStretch(1)
        layout.addLayout(title_row)
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
        # Long model names ("NZXT Kraken 2024 Elite RGB") wrap to a second
        # line instead of being elided — the user must see the full name.
        self._conn_pill.setWordWrap(True)
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

    #: D-Bus name of the StatusNotifierItem watcher provided by the panel.
    _SNI_SERVICE = "org.kde.StatusNotifierWatcher"
    #: Settle delay (ms) between the watcher appearing on the bus and creating
    #: the icon, giving the applet time to register its host side.
    _TRAY_CREATE_DELAY_MS = 1000
    #: Fallback polling cadence, used only when QtDBus is unavailable.
    _TRAY_RETRY_INTERVAL_MS = 2000
    _TRAY_RETRY_MAX_ATTEMPTS = 90

    def _build_tray(self) -> None:
        """Create the tray icon as soon as (and whenever) a tray host exists.

        Two hard-won rules drive this design (observed on COSMIC, 2026-06-02):

        1. ``QSystemTrayIcon.isSystemTrayAvailable()`` must NEVER be called
           before the panel's SNI watcher is on the bus — Qt caches the answer
           of its first "should we use the D-Bus tray" check for the process
           lifetime, so one early call breaks tray creation forever. We query
           the session bus directly instead (no cache).
        2. The panel applet can restart mid-session, silently dropping every
           registered item. A ``QDBusServiceWatcher`` on the watcher name lets
           us recreate the icon whenever a new host registers.
        """
        self._tray: QSystemTrayIcon | None = None
        self._tray_retry_attempts = 0
        self._tray_retry_timer: QTimer | None = None
        self._sni_watcher = None

        bus = None
        if _HAS_QTDBUS:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                bus = None
        if bus is None:
            # Last resort for exotic setups: poll Qt's own check. Unreliable
            # at login (see rule 1) but better than nothing.
            _LOGGER.info("QtDBus unavailable; falling back to polled tray detection.")
            timer = QTimer(self)
            timer.setInterval(self._TRAY_RETRY_INTERVAL_MS)
            timer.timeout.connect(self._retry_tray)
            timer.start()
            self._tray_retry_timer = timer
            return

        watcher = QDBusServiceWatcher(
            self._SNI_SERVICE,
            bus,
            QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange,
            self,
        )
        watcher.serviceOwnerChanged.connect(self._on_sni_owner_changed)
        self._sni_watcher = watcher

        if bus.interface().isServiceRegistered(self._SNI_SERVICE).value():
            self._create_tray()
        else:
            _LOGGER.info(
                "No tray host on the session bus yet; the tray icon will be "
                "created the moment one registers."
            )

    def _on_sni_owner_changed(self, service: str, old_owner: str, new_owner: str) -> None:
        """React to the tray host (dis)appearing or being replaced."""
        if new_owner:
            _LOGGER.info(
                "Tray host %s registered (owner %s); creating tray icon in %d ms.",
                service,
                new_owner,
                self._TRAY_CREATE_DELAY_MS,
            )
            QTimer.singleShot(self._TRAY_CREATE_DELAY_MS, self._recreate_tray)
        else:
            _LOGGER.info("Tray host %s vanished; dropping tray icon until it returns.", service)
            self._drop_tray()

    def _recreate_tray(self) -> None:
        """(Re)create the tray icon after the settle delay, if a host remains."""
        self._drop_tray()
        if _HAS_QTDBUS:
            bus = QDBusConnection.sessionBus()
            if not (
                bus.isConnected()
                and bus.interface().isServiceRegistered(self._SNI_SERVICE).value()
            ):
                return  # host vanished again while we waited
        self._create_tray()

    def _drop_tray(self) -> None:
        """Tear down the current tray icon (host gone or being replaced)."""
        if self._tray is not None:
            self._tray.hide()
            self._tray.deleteLater()
            self._tray = None

    def _retry_tray(self) -> None:
        """Fallback timer slot (no QtDBus): build the tray when Qt sees a host."""
        self._tray_retry_attempts += 1
        if QSystemTrayIcon.isSystemTrayAvailable():
            if self._tray_retry_timer is not None:
                self._tray_retry_timer.stop()
                self._tray_retry_timer = None
            _LOGGER.info(
                "Tray host appeared after %d attempt(s); creating tray icon.",
                self._tray_retry_attempts,
            )
            self._create_tray()
            return
        if self._tray_retry_attempts >= self._TRAY_RETRY_MAX_ATTEMPTS:
            if self._tray_retry_timer is not None:
                self._tray_retry_timer.stop()
                self._tray_retry_timer = None
            _LOGGER.info(
                "No tray host appeared after %d attempts; running without a "
                "tray icon (relaunch OpenKraken to open the window).",
                self._tray_retry_attempts,
            )

    def _create_tray(self) -> None:
        """Construct the tray icon, menu and wiring (host must be available)."""
        tray = QSystemTrayIcon(self)
        tray.setIcon(theme.make_tray_icon(None))
        tray.setToolTip("OpenKraken")

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
        # Force the next sample to repaint the icon: after a recreate the cached
        # integer temp may match the live one, which would otherwise skip the
        # update and leave the fresh icon temp-less.
        self._last_tray_temp = None
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
            self._tray.setToolTip(f"OpenKraken — liquid {temp:.0f}°C")
        else:
            self._tray.setToolTip("OpenKraken")

    def _on_connection_changed(self, connected: bool, description: str) -> None:
        if connected:
            label = description or "Kraken Elite"
            # Word wrap (set on the pill) shows the full model name across
            # lines; the tooltip is a belt-and-braces copy.
            self._conn_pill.setText(f"●  {label}")
            self._conn_pill.setToolTip(label)
        else:
            self._conn_pill.setText("●  Disconnected")
            self._conn_pill.setToolTip("")

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

    def restart_app(self) -> None:
        """Stop the engine and re-exec OpenKraken in place (for self-update).

        ``os.execv`` replaces this process image, so it keeps the same systemd
        unit / launch context — the freshly pulled code runs on next start.
        """
        import os
        import sys

        _LOGGER.info("Restarting OpenKraken to load the updated version.")
        self._really_quit = True
        try:
            self._engine.stop()
        except Exception:
            _LOGGER.exception("engine.stop() during restart failed (continuing)")
        try:
            os.execv(sys.executable, [sys.executable, "-m", "openkraken", *sys.argv[1:]])
        except Exception:
            _LOGGER.exception("os.execv restart failed; quitting instead")
            QApplication.quit()

    def _close_action(self) -> str:
        """Decide what closing the window should do.

        Returns one of:

        - ``"tray"`` — a tray exists and ``close_to_tray`` is set: hide to tray.
        - ``"background"`` — no tray but ``run_in_background`` is set: hide the
          window and keep the engine running (cooling/lighting/LCD stay active);
          re-launching OpenKraken reopens it.
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
            return

        if action == "background":
            event.ignore()
            self.hide()
            self._update_show_hide_label()
            if not MainWindow._logged_background_notice:
                MainWindow._logged_background_notice = True
                _LOGGER.info(
                    "running in background; launch OpenKraken again to reopen "
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
