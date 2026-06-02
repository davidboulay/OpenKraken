"""Control engine for Kraken CAM.

This module owns *all* periodic work in the application: device polling, system
sensor sampling, software-curve duty computation, LCD sensor-screen rendering and
reconnection handling.  It runs on its own :class:`~PyQt6.QtCore.QThread` so the GUI
thread never touches :class:`~krakencam.backend.device.KrakenDevice` or
:class:`~krakencam.backend.sensors.SystemSensors` directly.

The GUI interacts with the engine in exactly two ways:

* It listens to Qt signals (:data:`ControlEngine.sample_ready`,
  :data:`ControlEngine.connection_changed`, :data:`ControlEngine.applied`,
  :data:`ControlEngine.error`).  These are emitted from the engine thread and are
  auto-queued to the GUI thread by Qt.
* It calls the thread-safe request methods (:meth:`ControlEngine.apply_channel`,
  :meth:`ControlEngine.apply_lcd`, :meth:`ControlEngine.request_reconnect`,
  :meth:`ControlEngine.update_config`, :meth:`ControlEngine.stop`).  Each of these
  merely enqueues a closure onto an internal :class:`queue.Queue`; the run loop
  drains the queue (non-blocking) on every tick and performs the work itself.

The run loop wakes every ``config.poll_interval`` seconds but sleeps in slices of at
most :data:`_SLEEP_SLICE` so :meth:`ControlEngine.stop` is responsive.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Deque

from PyQt6.QtCore import QThread, pyqtSignal

from krakencam.backend import curves
from krakencam.backend import lcd_render
from krakencam.backend.device import DeviceStatus, KrakenDevice
from krakencam.backend.sensors import SystemSensors, SystemSnapshot
from krakencam.config import AppConfig, ChannelConfig, LcdConfig

_LOGGER = logging.getLogger(__name__)

# Maximum length of a single sleep slice, in seconds.  The run loop never blocks
# longer than this so ``stop()`` (which sets the stop event) is honoured promptly.
_SLEEP_SLICE: float = 0.2

# Minimum interval between reconnection attempts while disconnected, in seconds.
_RECONNECT_INTERVAL: float = 5.0

# Failsafe duty (percent) applied once when a software-curve source temperature is
# unavailable (sensor missing).  Keeps the loop alive without cooking it.
_FAILSAFE_DUTY: int = 80

# Timeout (seconds) for ``stop()`` to join the engine thread.
_STOP_TIMEOUT: float = 5.0

# Speed channels managed by the engine.
_CHANNELS: tuple[str, ...] = ("pump", "fan")


@dataclass
class _ChannelState:
    """Per-channel runtime state for software-curve control.

    For ``liquid``-source curves and ``fixed`` mode the firmware runs autonomously
    and this state is effectively idle (``mode``/``source`` recorded for bookkeeping
    only).  For ``cpu``/``gpu``-source curves the loop computes a duty every tick from
    ``points`` via :func:`curves.interpolate`, smooths it with ``smoother`` and writes
    a :func:`curves.software_failsafe` profile to the device whenever the smoothed
    duty changes.
    """

    mode: str = "curve"
    source: str = "liquid"
    points: list[tuple[float, int]] | None = None
    smoother: curves.DutySmoother | None = None
    # Whether this channel is driven tick-by-tick by software (cpu/gpu curve).
    software: bool = False
    # Set once we have emitted the "sensor missing" error so we do not spam it.
    failsafe_active: bool = False


class HistoryBuffers:
    """Thread-safe ring buffers of recent samples keyed by metric name.

    Each metric maps to a :class:`collections.deque` of ``(wall_time, value)`` pairs.
    ``wall_time`` is :func:`time.time` (suitable for x-axis plotting against a clock);
    values that are missing for a given sample are skipped (not stored as ``None``).

    All public methods take an internal lock so the GUI thread can call
    :meth:`series` while the engine thread calls :meth:`append`.
    """

    METRICS = [
        "liquid_temp",
        "cpu_temp",
        "gpu_temp",
        "pump_rpm",
        "fan_rpm",
        "pump_duty",
        "fan_duty",
        "cpu_load",
        "gpu_load",
    ]

    def __init__(self, seconds: int, interval: float) -> None:
        self._lock = threading.Lock()
        self._seconds = max(1, int(seconds))
        self._interval = max(0.05, float(interval))
        maxlen = self._compute_maxlen(self._seconds, self._interval)
        self._buffers: dict[str, Deque[tuple[float, float]]] = {
            metric: collections.deque(maxlen=maxlen) for metric in self.METRICS
        }

    @staticmethod
    def _compute_maxlen(seconds: int, interval: float) -> int:
        """Number of samples to retain for ``seconds`` of history at ``interval``."""
        return max(2, int(seconds / max(0.05, interval)) + 2)

    def append(self, status: DeviceStatus, snap: SystemSnapshot) -> None:
        """Append one sample's values from ``status`` and ``snap``.

        Missing (``None``) values are not appended for that metric.
        """
        now = time.time()
        values: dict[str, float | None] = {
            "liquid_temp": status.liquid_temp,
            "cpu_temp": snap.cpu_temp,
            "gpu_temp": snap.gpu_temp,
            "pump_rpm": status.pump_rpm,
            "fan_rpm": status.fan_rpm,
            "pump_duty": status.pump_duty,
            "fan_duty": status.fan_duty,
            "cpu_load": snap.cpu_load,
            "gpu_load": snap.gpu_load,
        }
        with self._lock:
            for metric, value in values.items():
                if value is None:
                    continue
                self._buffers[metric].append((now, float(value)))

    def series(self, metric: str) -> list[tuple[float, float]]:
        """Return a thread-safe copy of the ``(wall_time, value)`` series."""
        with self._lock:
            buf = self._buffers.get(metric)
            if buf is None:
                return []
            return list(buf)

    def resize(self, seconds: int, interval: float) -> None:
        """Resize all buffers for a new ``seconds`` window / sample ``interval``.

        Existing samples are preserved (truncated to the new capacity, keeping the
        most recent ones).
        """
        seconds = max(1, int(seconds))
        interval = max(0.05, float(interval))
        maxlen = self._compute_maxlen(seconds, interval)
        with self._lock:
            self._seconds = seconds
            self._interval = interval
            for metric, buf in self._buffers.items():
                self._buffers[metric] = collections.deque(buf, maxlen=maxlen)


class ControlEngine(QThread):
    """Periodic control loop running on its own thread.

    See the module docstring for the overall contract.  Construction does not start
    the thread or touch hardware; call :meth:`start` (inherited from ``QThread``) to
    begin.  Construction also does not require the device to be connected.
    """

    # ---- signals (emitted from the engine thread; auto-queued to the GUI) ----
    # (DeviceStatus, SystemSnapshot)
    sample_ready = pyqtSignal(object, object)
    # (connected, description)
    connection_changed = pyqtSignal(bool, str)
    # (what, detail), e.g. ("cooling", "pump: balanced curve")
    applied = pyqtSignal(str, str)
    # human-readable error message
    error = pyqtSignal(str)

    def __init__(
        self, device: KrakenDevice, sensors: SystemSensors, config: AppConfig
    ) -> None:
        super().__init__()
        self._device = device
        self._sensors = sensors
        self._config = config

        # GUI -> engine request queue; items are zero-argument callables executed on
        # the engine thread during the drain step of each tick.
        self._requests: "queue.Queue[Callable[[], None]]" = queue.Queue()

        # Stop signalling: set by stop(), checked in the sleep slices and loop guard.
        self._stop_event = threading.Event()

        self.history = HistoryBuffers(config.history_seconds, config.poll_interval)

        # Per-channel runtime state for software curves.
        self._channels: dict[str, _ChannelState] = {
            channel: _ChannelState() for channel in _CHANNELS
        }

        # Last connection state we emitted, so connection_changed only fires on
        # transitions.  None means "not yet emitted".
        self._last_connected: bool | None = None
        # Monotonic timestamp of the last reconnection attempt (0 == never).
        self._last_reconnect_attempt: float = 0.0

        # LCD runtime state.
        self._lcd_cfg: LcdConfig = config.lcd
        # Brightness to restore when leaving "off" mode (the configured value).
        self._lcd_saved_brightness: int = config.lcd.brightness
        # Whether we are currently in the emulated "off" state (brightness 0).
        self._lcd_off_active: bool = False
        # Monotonic timestamp of the last sensor-screen push (0 == never).
        self._last_lcd_push: float = 0.0
        # Latest brightness / orientation we successfully pushed, for change detection.
        self._applied_brightness: int | None = None
        self._applied_orientation: int | None = None

        # Most recent sample, used to seed software-curve duty from the *actual*
        # current source temperature when a cpu/gpu curve is applied.
        self._last_status: DeviceStatus | None = None
        self._last_snap: SystemSnapshot | None = None

    @property
    def device(self) -> KrakenDevice:
        """The underlying :class:`KrakenDevice`.

        Exposed read-only so the GUI (e.g. the Settings page) can display the
        model / firmware / connection state. ``KrakenDevice`` guards its
        ``description`` / ``is_connected`` / ``firmware_version`` reads with an
        ``RLock``, so GUI-thread access is safe.
        """
        return self._device

    # ------------------------------------------------------------------ #
    # Thread-safe request API (called from the GUI thread).
    # Each method only enqueues a closure; the work happens on the loop.
    # ------------------------------------------------------------------ #
    def apply_channel(self, channel: str, cfg: ChannelConfig) -> None:
        """Request that ``channel`` ("pump"/"fan") be (re)configured from ``cfg``.

        Thread-safe: enqueues the work onto the engine loop.
        """
        if channel not in self._channels:
            _LOGGER.error("apply_channel: unknown channel %r", channel)
            return
        # Snapshot the relevant fields so later GUI mutation of cfg cannot race.
        snapshot = ChannelConfig(
            mode=cfg.mode,
            source=cfg.source,
            fixed_duty=cfg.fixed_duty,
            points=list(cfg.points),
            profile=cfg.profile,
        )
        self._requests.put(lambda: self._do_apply_channel(channel, snapshot))

    def apply_lcd(self, cfg: LcdConfig) -> None:
        """Request that the LCD be (re)configured from ``cfg``.

        Thread-safe: enqueues the work onto the engine loop.
        """
        snapshot = LcdConfig(
            mode=cfg.mode,
            brightness=cfg.brightness,
            orientation=cfg.orientation,
            image_path=cfg.image_path,
            gif_path=cfg.gif_path,
            sensor_style=cfg.sensor_style,
            sensor_interval=cfg.sensor_interval,
        )
        self._requests.put(lambda: self._do_apply_lcd(snapshot))

    def request_reconnect(self) -> None:
        """Request an immediate reconnection attempt.

        Thread-safe: enqueues the work onto the engine loop.
        """
        self._requests.put(self._do_request_reconnect)

    def update_config(self, config: AppConfig) -> None:
        """Request that loop timing / history sizing be updated from ``config``.

        Thread-safe: enqueues the work onto the engine loop.  Does not re-apply
        channel or LCD configs (use :meth:`apply_channel` / :meth:`apply_lcd`).
        """
        self._requests.put(lambda: self._do_update_config(config))

    def stop(self) -> None:
        """Stop the loop and wait (up to :data:`_STOP_TIMEOUT`) for it to finish.

        Safe to call from the GUI thread.  Idempotent.
        """
        _LOGGER.info("engine stop requested")
        self._stop_event.set()
        # QThread.wait expects milliseconds.
        if self.isRunning():
            if not self.wait(int(_STOP_TIMEOUT * 1000)):
                _LOGGER.warning(
                    "engine thread did not stop within %.1f s", _STOP_TIMEOUT
                )

    # ------------------------------------------------------------------ #
    # The run loop.
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """Main control loop; runs on the engine thread until :meth:`stop`."""
        _LOGGER.info("control engine started")
        try:
            # Initial connection + apply-on-start.
            self._startup()
            while not self._stop_event.is_set():
                tick_start = time.monotonic()

                # 1. Reconnect handling (at most every _RECONNECT_INTERVAL).
                self._maybe_reconnect()

                # 2. Sample status + sensors, record history, emit to GUI.
                status, snap = self._sample()

                # 3. Drive software-curve channels for this tick.
                self._tick_software_curves(status, snap)

                # 4. Push an LCD sensor frame if due.
                self._tick_lcd_sensors(status, snap)

                # 5. Drain queued GUI requests.
                self._drain_requests()

                # Sleep the remainder of poll_interval in small slices.
                self._sleep_until(tick_start + self._config.poll_interval)
        except Exception:  # pragma: no cover - defensive; loop must never crash silently
            _LOGGER.exception("control engine crashed")
        finally:
            self._device.disconnect()
            _LOGGER.info("control engine stopped")

    # ------------------------------------------------------------------ #
    # Startup.
    # ------------------------------------------------------------------ #
    def _startup(self) -> None:
        """Connect the device and, if configured, apply saved settings on start."""
        self._last_reconnect_attempt = time.monotonic()
        connected = self._device.connect()
        self._emit_connection(connected)
        if connected and self._config.apply_on_start:
            self._apply_all_configs()

    def _apply_all_configs(self) -> None:
        """(Re)apply pump, fan and LCD configs from the current AppConfig.

        Called on startup (when ``apply_on_start``) and automatically after every
        successful (re)connection.
        """
        # A (re)connected device comes back at its own firmware-stored brightness /
        # orientation. Our change-detection cache (``_applied_*``) is *not* cleared
        # on disconnect, so without resetting it here _set_brightness/_set_orientation
        # would short-circuit and silently leave the LCD at the firmware defaults.
        # Force a re-write by forgetting the last-applied values.
        self._applied_brightness = None
        self._applied_orientation = None
        self._do_apply_channel("pump", self._config.pump)
        self._do_apply_channel("fan", self._config.fan)
        self._do_apply_lcd(self._lcd_cfg)

    # ------------------------------------------------------------------ #
    # Loop step 1: reconnection.
    # ------------------------------------------------------------------ #
    def _maybe_reconnect(self) -> None:
        """Attempt reconnection at most every :data:`_RECONNECT_INTERVAL` seconds.

        Emits :data:`connection_changed` only on a connection-state transition, and
        re-applies the current channel + LCD configs after a successful reconnect.
        """
        if self._device.is_connected:
            return
        now = time.monotonic()
        if now - self._last_reconnect_attempt < _RECONNECT_INTERVAL:
            return
        self._last_reconnect_attempt = now
        _LOGGER.debug("attempting device reconnection")
        if self._device.connect():
            _LOGGER.info("device reconnected")
            self._emit_connection(True)
            # Re-apply everything so the firmware/LCD reflect the desired state.
            self._apply_all_configs()
        else:
            # Still down; ensure GUI knows (first time only, handled by transition).
            self._emit_connection(False)

    def _emit_connection(self, connected: bool) -> None:
        """Emit :data:`connection_changed` only when the state actually changes."""
        if connected == self._last_connected:
            return
        self._last_connected = connected
        description = self._device.description if connected else ""
        _LOGGER.info(
            "connection state changed: connected=%s (%s)", connected, description
        )
        self.connection_changed.emit(connected, description)

    # ------------------------------------------------------------------ #
    # Loop step 2: sampling.
    # ------------------------------------------------------------------ #
    def _sample(self) -> tuple[DeviceStatus, SystemSnapshot]:
        """Read device status + system sensors, append history, emit sample_ready."""
        status = self._device.get_status()
        snap = self._sensors.read()
        self._last_status = status
        self._last_snap = snap
        # get_status() marks the device disconnected on I/O error; reflect that.
        if not status.connected:
            self._emit_connection(False)
        self.history.append(status, snap)
        self.sample_ready.emit(status, snap)
        return status, snap

    # ------------------------------------------------------------------ #
    # Loop step 3: software curves.
    # ------------------------------------------------------------------ #
    def _source_temp(
        self, source: str, status: DeviceStatus, snap: SystemSnapshot
    ) -> float | None:
        """Return the temperature for a curve ``source`` ("liquid"/"cpu"/"gpu")."""
        if source == "cpu":
            return snap.cpu_temp
        if source == "gpu":
            return snap.gpu_temp
        return status.liquid_temp

    def _tick_software_curves(
        self, status: DeviceStatus, snap: SystemSnapshot
    ) -> None:
        """Compute and write software-curve duties for cpu/gpu-source channels.

        Firmware-native (liquid-source) curves and fixed mode are not driven here;
        they were applied once via :meth:`_do_apply_channel`.
        """
        if not self._device.is_connected:
            return
        for channel, state in self._channels.items():
            if not state.software or state.smoother is None or state.points is None:
                continue
            source_temp = self._source_temp(state.source, status, snap)
            if source_temp is None:
                # Sensor missing: apply failsafe 80% once, emit one error.
                self._apply_software_failsafe_missing(channel, state)
                continue
            # Source recovered; clear the latched failsafe flag.
            if state.failsafe_active:
                state.failsafe_active = False
            target = curves.interpolate(state.points, float(source_temp))
            duty = state.smoother.update(target)
            if duty is None:
                continue  # within deadband; nothing to write
            profile = curves.software_failsafe(duty, channel)
            if self._device.set_speed_profile(channel, profile):
                _LOGGER.debug(
                    "%s software curve: source=%s temp=%.1f -> duty=%d%%",
                    channel,
                    state.source,
                    source_temp,
                    duty,
                )
            else:
                _LOGGER.warning("failed to write software curve for %s", channel)
                self._emit_connection(self._device.is_connected)

    def _apply_software_failsafe_missing(
        self, channel: str, state: _ChannelState
    ) -> None:
        """Apply the missing-sensor failsafe (80%) once, emitting one error signal."""
        if state.failsafe_active:
            return  # already handled; do not spam every tick
        state.failsafe_active = True
        _LOGGER.error(
            "source temperature for %s curve is unavailable; applying %d%% failsafe",
            channel,
            _FAILSAFE_DUTY,
        )
        self.error.emit(
            f"{channel}: source sensor unavailable, applied {_FAILSAFE_DUTY}% failsafe"
        )
        if state.smoother is not None:
            # Reset so the smoother does not block the failsafe write.
            state.smoother.reset()
        profile = curves.software_failsafe(_FAILSAFE_DUTY, channel)
        self._device.set_speed_profile(channel, profile)

    # ------------------------------------------------------------------ #
    # Loop step 4: LCD sensor frames.
    # ------------------------------------------------------------------ #
    def _tick_lcd_sensors(self, status: DeviceStatus, snap: SystemSnapshot) -> None:
        """Render and push an LCD sensor frame if in "sensors" mode and due."""
        if self._lcd_cfg.mode != "sensors":
            return
        now = time.monotonic()
        if now - self._last_lcd_push < self._lcd_cfg.sensor_interval:
            return
        self._last_lcd_push = now
        if not self._device.is_connected:
            _LOGGER.debug("skipping LCD sensor push: device disconnected")
            return
        data = lcd_render.LcdData(
            liquid_temp=status.liquid_temp,
            cpu_temp=snap.cpu_temp,
            cpu_load=snap.cpu_load,
            gpu_temp=snap.gpu_temp,
            gpu_load=snap.gpu_load,
            pump_rpm=status.pump_rpm,
            fan_rpm=status.fan_rpm,
        )
        try:
            path = lcd_render.render_to_file(self._lcd_cfg.sensor_style, data)
        except Exception:  # pragma: no cover - rendering must not crash the loop
            _LOGGER.exception("failed to render LCD sensor frame")
            return
        if not self._device.set_lcd_static(path):
            _LOGGER.warning("failed to push LCD sensor frame")
            self._emit_connection(self._device.is_connected)

    # ------------------------------------------------------------------ #
    # Loop step 5: request draining.
    # ------------------------------------------------------------------ #
    def _drain_requests(self) -> None:
        """Execute all queued GUI requests (non-blocking)."""
        while True:
            try:
                work = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                work()
            except Exception:  # pragma: no cover - a bad request must not crash loop
                _LOGGER.exception("engine request raised")

    # ------------------------------------------------------------------ #
    # Request implementations (run on the engine thread).
    # ------------------------------------------------------------------ #
    def _do_apply_channel(self, channel: str, cfg: ChannelConfig) -> None:
        """Apply a channel config on the engine thread.

        * ``fixed`` mode -> :meth:`KrakenDevice.set_fixed_speed` once (firmware clamps).
        * ``curve`` + ``liquid`` source -> :meth:`KrakenDevice.set_speed_profile`
          once; the firmware then runs the curve autonomously.
        * ``curve`` + ``cpu``/``gpu`` source -> store points + fresh DutySmoother; the
          run loop computes the duty each tick and writes a software-failsafe profile
          when it changes.
        """
        state = self._channels[channel]
        # Keep the engine's view of the persistent config in sync so reconnect
        # re-applies the latest requested settings.
        self._store_channel_cfg(channel, cfg)

        state.mode = cfg.mode
        state.source = cfg.source
        # Re-applying always resets runtime state (fresh smoother, cleared failsafe).
        state.failsafe_active = False

        if cfg.mode == "fixed":
            state.software = False
            state.points = None
            state.smoother = None
            ok = self._device.set_fixed_speed(channel, cfg.fixed_duty)
            detail = f"{channel}: fixed {cfg.fixed_duty}%"
            self._emit_apply_result("cooling", detail, ok)
            return

        # Curve mode.
        points = curves.validate_points(cfg.points)
        state.points = points

        if cfg.source == "liquid":
            # Firmware-native: write once, runs autonomously.
            state.software = False
            state.smoother = None
            ok = self._device.set_speed_profile(channel, points)
            detail = f"{channel}: {cfg.profile} curve (liquid)"
            self._emit_apply_result("cooling", detail, ok)
            return

        # Software curve driven from cpu/gpu temperature.
        state.software = True
        state.smoother = curves.DutySmoother()
        # Seed an immediate software-failsafe write from the channel's *actual*
        # current source temperature so the cooler is in a sane state right away.
        # The curve x-axis is the cpu/gpu temperature, NOT liquid temperature, so
        # we must sample it at the real source reading -- evaluating it at the
        # liquid CRITICAL_TEMP (59) would seed a bogus duty (e.g. a steep curve
        # would seed 100%) that then decays slowly via the smoother, causing an
        # audible over-spin on every apply/reconnect.  If no sample is available
        # yet, defer the first write to the next tick, which uses the live temp.
        ok = True
        if self._device.is_connected:
            source_temp = self._initial_source_temp(cfg.source)
            if source_temp is not None:
                initial = curves.interpolate(points, float(source_temp))
                initial_duty = state.smoother.update(initial)
                if initial_duty is not None:
                    ok = self._device.set_speed_profile(
                        channel, curves.software_failsafe(initial_duty, channel)
                    )
        detail = f"{channel}: {cfg.profile} curve ({cfg.source} software)"
        self._emit_apply_result("cooling", detail, ok)

    def _initial_source_temp(self, source: str) -> float | None:
        """Latest known temperature for a curve ``source`` ("cpu"/"gpu"/"liquid").

        Used only to seed the immediate software-curve write in
        :meth:`_do_apply_channel`; returns ``None`` when no sample has been taken
        yet (the loop then defers the first write to the next tick).
        """
        if self._last_status is None or self._last_snap is None:
            return None
        return self._source_temp(source, self._last_status, self._last_snap)

    def _store_channel_cfg(self, channel: str, cfg: ChannelConfig) -> None:
        """Mirror an applied channel config into the engine's AppConfig copy."""
        if channel == "pump":
            self._config.pump = cfg
        elif channel == "fan":
            self._config.fan = cfg

    def _do_apply_lcd(self, cfg: LcdConfig) -> None:
        """Apply an LCD config on the engine thread.

        Handles the emulated "off" mode (brightness 0, remembering the configured
        brightness for restoration), applies brightness/orientation when changed, and
        sets the content mode (liquid/static/gif/sensors).
        """
        previous_mode = self._lcd_cfg.mode
        self._lcd_cfg = cfg
        self._config.lcd = cfg

        if not self._device.is_connected:
            _LOGGER.debug("apply_lcd deferred: device disconnected")
            return

        # Reset the sensor push timer so a fresh frame is pushed promptly when we
        # (re)enter sensors mode.
        if cfg.mode == "sensors":
            self._last_lcd_push = 0.0

        # ---- brightness handling, including the "off" emulation ----
        if cfg.mode == "off":
            # Remember the configured (non-zero) brightness for restoration.
            self._lcd_saved_brightness = cfg.brightness
            self._lcd_off_active = True
            # Orientation does not depend on the panel being lit; still apply it so
            # an orientation change made while the screen is off is not dropped.
            self._set_orientation(cfg.orientation)
            ok_b = self._set_brightness(0)
            self._emit_apply_result("lcd", "screen off", ok_b)
            return

        # Leaving "off" (or simply not in off): ensure brightness reflects config.
        leaving_off = self._lcd_off_active
        self._lcd_off_active = False
        self._lcd_saved_brightness = cfg.brightness
        target_brightness = cfg.brightness
        if leaving_off:
            # Force a brightness write to restore from the 0 we set while off.
            self._applied_brightness = None
        self._set_brightness(target_brightness)

        # ---- orientation (applied when changed) ----
        self._set_orientation(cfg.orientation)

        # ---- content mode ----
        if cfg.mode == "liquid":
            ok = self._device.set_lcd_liquid_mode()
            self._emit_apply_result("lcd", "liquid temperature screen", ok)
        elif cfg.mode == "static":
            if cfg.image_path:
                ok = self._device.set_lcd_static(cfg.image_path)
                self._emit_apply_result("lcd", f"static image {cfg.image_path}", ok)
            else:
                _LOGGER.warning("LCD static mode requested without an image path")
                self.error.emit("LCD: no static image selected")
        elif cfg.mode == "gif":
            if cfg.gif_path:
                ok = self._device.set_lcd_gif(cfg.gif_path)
                self._emit_apply_result("lcd", f"animated GIF {cfg.gif_path}", ok)
            else:
                _LOGGER.warning("LCD gif mode requested without a gif path")
                self.error.emit("LCD: no GIF selected")
        elif cfg.mode == "sensors":
            # Content is pushed by the loop; nothing to do here beyond brightness.
            self._emit_apply_result(
                "lcd", f"sensor screen ({cfg.sensor_style})", True
            )
        else:
            _LOGGER.warning("unknown LCD mode %r (was %r)", cfg.mode, previous_mode)

    def _set_brightness(self, value: int) -> bool:
        """Set LCD brightness only if it differs from the last applied value."""
        if self._applied_brightness == value:
            return True
        ok = self._device.set_lcd_brightness(value)
        if ok:
            self._applied_brightness = value
        else:
            _LOGGER.warning("failed to set LCD brightness to %d", value)
        return ok

    def _set_orientation(self, degrees: int) -> bool:
        """Set LCD orientation only if it differs from the last applied value."""
        if self._applied_orientation == degrees:
            return True
        ok = self._device.set_lcd_orientation(degrees)
        if ok:
            self._applied_orientation = degrees
        else:
            _LOGGER.warning("failed to set LCD orientation to %d", degrees)
        return ok

    def _emit_apply_result(self, what: str, detail: str, ok: bool) -> None:
        """Emit :data:`applied` on success or :data:`error` on failure."""
        if ok:
            _LOGGER.info("applied %s: %s", what, detail)
            self.applied.emit(what, detail)
        else:
            _LOGGER.warning("failed to apply %s: %s", what, detail)
            self.error.emit(f"Failed to apply {what}: {detail}")
            # An I/O failure marks the device disconnected; reflect any transition.
            self._emit_connection(self._device.is_connected)

    def _do_request_reconnect(self) -> None:
        """Force an immediate reconnection attempt (resets the backoff timer)."""
        _LOGGER.info("manual reconnect requested")
        if self._device.is_connected:
            # Already connected; re-apply to be safe and report.
            self._apply_all_configs()
            return
        # Reset the backoff so _maybe_reconnect tries on the next tick, but also try
        # now for snappier UX.
        self._last_reconnect_attempt = 0.0
        self._maybe_reconnect()

    def _do_update_config(self, config: AppConfig) -> None:
        """Update loop timing and resize history buffers from ``config``."""
        old_interval = self._config.poll_interval
        old_seconds = self._config.history_seconds
        self._config = config
        # Keep the LCD reference coherent (timing/window only here).
        if (
            config.poll_interval != old_interval
            or config.history_seconds != old_seconds
        ):
            self.history.resize(config.history_seconds, config.poll_interval)
            _LOGGER.info(
                "config updated: poll_interval=%.2fs history=%ds",
                config.poll_interval,
                config.history_seconds,
            )

    # ------------------------------------------------------------------ #
    # Sleep helper.
    # ------------------------------------------------------------------ #
    def _sleep_until(self, deadline: float) -> None:
        """Sleep until ``deadline`` (monotonic) in <= :data:`_SLEEP_SLICE` slices.

        Returns early if :meth:`stop` is requested.
        """
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # Event.wait both sleeps and wakes promptly when stop() sets the event.
            self._stop_event.wait(min(_SLEEP_SLICE, remaining))
