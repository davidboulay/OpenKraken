"""Application bootstrap and ``main()`` entry point for OpenKraken.

Parses CLI arguments, configures logging, builds the Qt application, wires up
the backend (device + sensors + control engine) and the main window, and runs
the event loop. The control engine is started only *after* the window is
constructed and its signals are connected, so the very first
``connection_changed`` emission is not missed.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from openkraken import __version__
from openkraken.backend.device import KrakenDevice
from openkraken.backend.engine import ControlEngine
from openkraken.backend.sensors import SystemSensors
from openkraken.config import AppConfig
from openkraken.gui import theme
from openkraken.gui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

# How often the SIGINT-keepalive timer fires so the CPython interpreter gets a
# chance to run installed Python signal handlers while inside the Qt event loop.
_SIGNAL_TICK_MS = 200

# How long a probing QLocalSocket waits to connect to an already-running instance
# before deciding that we are the first/only instance.
_INSTANCE_CONNECT_TIMEOUT_MS = 200

# Messages exchanged over the single-instance local socket. "activate" asks the
# running instance to raise its window; "ping" only signals presence (used by the
# autostart --minimized path so an autostart double-fire never pops the window).
_MSG_ACTIVATE = b"activate\n"
_MSG_PING = b"ping\n"


def _instance_server_name() -> str:
    """Return the per-user QLocalServer name for the single-instance lock."""
    return f"openkraken-{os.getuid()}"


def _notify_running_instance(name: str, *, activate: bool) -> bool:
    """Try to hand off to an already-running instance over its local socket.

    Connects to the named local server (waiting up to
    :data:`_INSTANCE_CONNECT_TIMEOUT_MS`). On success, writes ``activate`` (to
    raise the running window) or ``ping`` (presence only, no raise), flushes,
    disconnects and returns ``True``. Returns ``False`` when no instance owns the
    socket (so the caller should become the primary instance).
    """
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(_INSTANCE_CONNECT_TIMEOUT_MS):
        return False

    socket.write(_MSG_ACTIVATE if activate else _MSG_PING)
    socket.flush()
    socket.waitForBytesWritten(_INSTANCE_CONNECT_TIMEOUT_MS)
    socket.disconnectFromServer()
    return True


def setup_single_instance(
    name: str, on_activate: Callable[[], None]
) -> QLocalServer | None:
    """Become the primary instance by listening on a per-user local socket.

    Returns a listening :class:`QLocalServer` whose ``newConnection`` reads a
    single command line: ``activate`` invokes ``on_activate`` (raise the window),
    while ``ping`` is ignored.

    ``listen()`` is attempted *first*: if it fails with ``AddressInUseError`` the
    name is already taken. We only :meth:`QLocalServer.removeServer` and retry
    when a connect-probe (:func:`_notify_running_instance`) confirms that nobody
    is actually listening — i.e. the file is a stale socket left by a crashed
    previous instance. This preserves crash recovery while keeping the OS-level
    guarantee that a *live* second instance cannot steal the name (which an
    unconditional ``removeServer`` would defeat, leaving two "primaries").

    Returns ``None`` when another live instance already owns the socket — in that
    case the caller has typically already handed off via
    :func:`_notify_running_instance` and should exit.
    """
    server = QLocalServer()
    if not server.listen(name):
        if server.serverError() != QAbstractSocket.SocketError.AddressInUseError:
            _LOGGER.warning(
                "Could not listen on single-instance socket %r: %s",
                name,
                server.errorString(),
            )
            return None

        # The name is taken. Probe it: if a live instance answers, defer to it.
        # Otherwise the socket file is stale (crash) — remove it and retry once.
        if _notify_running_instance(name, activate=False):
            _LOGGER.debug(
                "Single-instance socket %r is owned by a live instance.", name
            )
            return None

        _LOGGER.info("Removing stale single-instance socket %r and retrying.", name)
        QLocalServer.removeServer(name)
        if not server.listen(name):
            _LOGGER.warning(
                "Could not listen on single-instance socket %r after clearing a "
                "stale socket: %s",
                name,
                server.errorString(),
            )
            return None

    def _on_new_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return
        # Read whatever line the peer sent (it disconnects right after writing).
        if not connection.waitForReadyRead(_INSTANCE_CONNECT_TIMEOUT_MS):
            connection.deleteLater()
            return
        command = bytes(connection.readAll()).strip()
        if command == _MSG_ACTIVATE.strip():
            _LOGGER.info("Activation request received from a second instance.")
            on_activate()
        else:
            _LOGGER.debug("Ignoring single-instance message %r.", command)
        connection.disconnectFromServer()
        connection.deleteLater()

    server.newConnection.connect(_on_new_connection)
    return server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openkraken",
        description="Linux clone of NZXT CAM for the NZXT Kraken 2024 Elite RGB.",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help=(
            "start hidden: in the system tray if one is available, otherwise in "
            "the background without a window"
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=Path,
        default=None,
        help="path to the config JSON file (defaults to the standard location)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable DEBUG-level logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"openkraken {__version__}",
    )
    return parser


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    # liquidctl can be chatty about missing hwmon; keep it quiet unless debugging.
    if not debug:
        logging.getLogger("liquidctl").setLevel(logging.WARNING)


def _install_sigint_handler(app: QApplication, engine: ControlEngine) -> QTimer:
    """Install a clean Ctrl-C handler.

    Qt's event loop blocks in C, so Python-level signal handlers do not run
    while it is idle. A no-op :class:`QTimer` that fires periodically returns
    control to the interpreter often enough for the handler to be delivered.
    """

    def _handle_sigint(_signum: int, _frame: object) -> None:
        _LOGGER.info("SIGINT received; shutting down.")
        engine.stop()
        app.quit()

    signal.signal(signal.SIGINT, _handle_sigint)

    timer = QTimer()
    timer.setInterval(_SIGNAL_TICK_MS)
    timer.timeout.connect(lambda: None)  # wake the interpreter; do nothing else
    timer.start()
    return timer


def main(argv: list[str] | None = None) -> int:
    """Run the OpenKraken application.

    Parameters
    ----------
    argv:
        Optional argument list (excluding the program name). Defaults to
        ``sys.argv[1:]``.

    Returns
    -------
    int
        The Qt event-loop exit code.
    """
    args = _build_parser().parse_args(argv)
    _configure_logging(args.debug)

    _LOGGER.info("Starting OpenKraken %s", __version__)

    config = AppConfig.load(args.config)
    start_minimized = bool(args.minimized or config.start_minimized)

    app = QApplication(sys.argv[:1] + (argv or sys.argv[1:]))
    app.setApplicationName("openkraken")
    app.setApplicationDisplayName("OpenKraken")
    app.setDesktopFileName("openkraken")
    # Keep running when the last window is hidden to tray.
    app.setQuitOnLastWindowClosed(False)

    # --- Single instance: hand off to an already-running copy ----------------
    # A normal launch raises the running window; only the autostart --minimized
    # FLAG pings (presence only) so an autostart double-fire never pops the
    # window. This is keyed off args.minimized -- NOT the combined
    # start_minimized -- so a user who persists config.start_minimized=True can
    # still reopen the window by relaunching (an important escape hatch on
    # no-tray desktops, where Ctrl+Q only works while the window is shown).
    instance_name = _instance_server_name()
    if _notify_running_instance(instance_name, activate=not args.minimized):
        _LOGGER.info(
            "Another OpenKraken instance is already running; %s it.",
            "pinged" if args.minimized else "activated",
        )
        return 0

    theme.apply_theme(app)
    app.setWindowIcon(theme.make_app_icon())

    # --- Backend (no hardware access until the engine starts) ---------------
    device = KrakenDevice()
    sensors = SystemSensors()
    engine = ControlEngine(device, sensors, config)

    # --- GUI -----------------------------------------------------------------
    window = MainWindow(engine, config)

    # Become the primary instance: listen for activation requests from future
    # launches (raise the window). Parented to the app and closed on shutdown.
    instance_server = setup_single_instance(instance_name, window.show_and_raise)
    if instance_server is not None:
        instance_server.setParent(app)
        app.aboutToQuit.connect(instance_server.close)

    # Decide visibility before starting the engine so we don't flash the window.
    # When minimized is requested the window stays hidden even if no tray is
    # available yet: at login the app races the panel's tray host (it usually
    # appears a few seconds later and MainWindow retries), and the window is
    # always reachable by relaunching (single-instance activation) or via the
    # tray once it materializes.
    if start_minimized:
        if QSystemTrayIcon.isSystemTrayAvailable():
            _LOGGER.info("Starting minimized to tray.")
        else:
            _LOGGER.info(
                "Starting minimized; no tray host yet (will keep retrying). "
                "Relaunch OpenKraken to open the window."
            )
    else:
        window.show()

    # Start the engine only after the window's signals are connected so the
    # initial connection_changed / sample_ready emissions are delivered.
    engine.start()

    # Clean shutdown paths.
    app.aboutToQuit.connect(engine.stop)
    _signal_timer = _install_sigint_handler(app, engine)
    # Keep a reference so the timer is not garbage-collected.
    app.setProperty("_kraken_signal_timer", id(_signal_timer))

    exit_code = app.exec()
    _LOGGER.info("Event loop exited with code %d", exit_code)
    # Belt-and-braces: ensure the engine thread is stopped before returning.
    engine.stop()
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
