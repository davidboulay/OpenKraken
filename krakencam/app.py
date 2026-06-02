"""Application bootstrap and ``main()`` entry point for Kraken CAM.

Parses CLI arguments, configures logging, builds the Qt application, wires up
the backend (device + sensors + control engine) and the main window, and runs
the event loop. The control engine is started only *after* the window is
constructed and its signals are connected, so the very first
``connection_changed`` emission is not missed.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from krakencam import __version__
from krakencam.backend.device import KrakenDevice
from krakencam.backend.engine import ControlEngine
from krakencam.backend.sensors import SystemSensors
from krakencam.config import AppConfig
from krakencam.gui import theme
from krakencam.gui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

# How often the SIGINT-keepalive timer fires so the CPython interpreter gets a
# chance to run installed Python signal handlers while inside the Qt event loop.
_SIGNAL_TICK_MS = 200


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kraken-cam",
        description="Linux clone of NZXT CAM for the NZXT Kraken 2024 Elite RGB.",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="start hidden in the system tray (if a tray is available)",
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
        version=f"kraken-cam {__version__}",
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
    """Run the Kraken CAM application.

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

    _LOGGER.info("Starting Kraken CAM %s", __version__)

    config = AppConfig.load(args.config)
    start_minimized = bool(args.minimized or config.start_minimized)

    app = QApplication(sys.argv[:1] + (argv or sys.argv[1:]))
    app.setApplicationName("kraken-cam")
    app.setApplicationDisplayName("Kraken CAM")
    app.setDesktopFileName("kraken-cam")
    # Keep running when the last window is hidden to tray.
    app.setQuitOnLastWindowClosed(False)

    theme.apply_theme(app)
    app.setWindowIcon(theme.make_app_icon())

    # --- Backend (no hardware access until the engine starts) ---------------
    device = KrakenDevice()
    sensors = SystemSensors()
    engine = ControlEngine(device, sensors, config)

    # --- GUI -----------------------------------------------------------------
    window = MainWindow(engine, config)

    # Decide visibility before starting the engine so we don't flash the window.
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    if start_minimized and tray_available:
        _LOGGER.info("Starting minimized to tray.")
    else:
        if start_minimized and not tray_available:
            _LOGGER.warning(
                "--minimized requested but no system tray is available; "
                "showing the window instead."
            )
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
