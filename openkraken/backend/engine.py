"""Control engine for OpenKraken.

This module owns *all* periodic work in the application: device polling, system
sensor sampling, software-curve duty computation, LCD sensor-screen rendering and
reconnection handling.  It runs on its own :class:`~PyQt6.QtCore.QThread` so the GUI
thread never touches :class:`~openkraken.backend.device.KrakenDevice` or
:class:`~openkraken.backend.sensors.SystemSensors` directly.

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

from openkraken.backend import curves
from openkraken.backend import lcd_render
from openkraken.backend import lighting_fx
from openkraken.backend.device import DeviceStatus, KrakenDevice
from openkraken.config import AppConfig, ChannelConfig, LcdConfig, LightingChannelConfig, LightingConfig

_LOGGER = logging.getLogger(__name__)

# Maximum length of a single sleep slice, in seconds.  The run loop never blocks
# longer than this so ``stop()`` (which sets the stop event) is honoured promptly.
_SLEEP_SLICE: float = 0.2

# Minimum interval between reconnection attempts while disconnected, in seconds.
_RECONNECT_INTERVAL: float = 5.0

# System-uptime threshold (seconds) below which an engine start counts as "during
# boot", enabling the LCD startup grace delay. A manual restart later (uptime above
# this) applies the LCD immediately so it recovers a black screen on the spot.
_LCD_GRACE_BOOT_WINDOW: float = 120.0

# Failsafe duty (percent) applied once when a software-curve source temperature is
# unavailable (sensor missing).  Keeps the loop alive without cooking it.
_FAILSAFE_DUTY: int = 80

# Timeout (seconds) for ``stop()`` to join the engine thread.
_STOP_TIMEOUT: float = 5.0

# Speed channels managed by the engine.
_CHANNELS: tuple[str, ...] = ("pump", "fan")

# RGB lighting channels managed by the engine (names match config / device masks).
_LIGHTING_CHANNELS: tuple[str, ...] = ("ring", "fans")

# Fallback minimum interval between successive lighting frame writes for an
# animated mode, in seconds, when ``lighting_fps`` is unset/invalid.  PROTOCOL.md
# §5 relayed OpenRGB's ~1 FPS claim, but a live soak (5 FPS, ring+fans, 450
# writes, zero failures) showed the device accepts faster streaming from our
# write path — the actual cadence comes from ``config.lighting_fps`` via
# :meth:`ControlEngine._lighting_write_interval` (clamped to 0.5..5 FPS).
_LIGHTING_MIN_WRITE_INTERVAL: float = 1.0

# lighting_fps clamp bounds: 5 FPS is the soak-tested ceiling; 0.5 FPS is a
# floor so a bogus config value can't freeze animations near-solid.
_LIGHTING_FPS_MIN: float = 0.5
_LIGHTING_FPS_MAX: float = 5.0

# Fallback per-channel LED counts used until ``device.lighting_info`` is populated
# (PROTOCOL.md §2 / INTERFACES-LIGHTING.md): the 2024 Elite ring is 24 LEDs and a
# typical RGB Core fan chain is 16 (2 x 8-LED fans).
_LIGHTING_FALLBACK_LEDS: dict[str, int] = {"ring": 24, "fans": 16}


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


@dataclass
class _LightingState:
    """Per-channel runtime state for an RGB lighting channel (``ring``/``fans``).

    Captures the effect parameters applied to this channel plus the bookkeeping the
    run loop needs to stream animated frames at ~1 FPS:

    * ``animated`` -- whether the active ``mode`` is a host-streamed animation
      (from :data:`lighting_fx.MODES`).  Non-animated modes are written once at
      apply time and never touched again by the loop.
    * ``origin`` -- ``time.monotonic()`` captured when the mode was (re)applied; the
      elapsed time ``t = now - origin`` is fed to :func:`lighting_fx.frame`.
    * ``last_write`` -- monotonic timestamp of the last animated frame written, so
      the loop honours :data:`_LIGHTING_MIN_WRITE_INTERVAL` (the device's ~1 FPS
      ceiling) regardless of ``poll_interval``.
    """

    mode: str = "off"
    colors: list[tuple[int, int, int]] | None = None
    brightness: int = 100
    speed: str = "normal"
    animated: bool = False
    origin: float = 0.0
    last_write: float = 0.0


def _copy_lighting_channel(cfg: LightingChannelConfig) -> LightingChannelConfig:
    """Deep-copy a :class:`LightingChannelConfig` (colours copied as fresh tuples).

    Used to snapshot a GUI-owned config when queuing :meth:`ControlEngine.apply_lighting`
    so later GUI mutation cannot race the engine thread.
    """
    return LightingChannelConfig(
        mode=cfg.mode,
        colors=[tuple(c) for c in cfg.colors],
        brightness=cfg.brightness,
        speed=cfg.speed,
    )


def _speed_time_warp(speed: str) -> float:
    """Time-warp factor for an animation ``speed`` ("slow"/"normal"/"fast").

    Returns ``normal_period / speed_period`` from :data:`lighting_fx.SPEED_PERIODS`,
    so multiplying elapsed time by it makes a faster speed advance the effect phase
    proportionally faster.  Unknown speeds (or a missing/zero "normal" reference)
    fall back to ``1.0`` so a bad config never breaks the time axis.
    """
    periods = lighting_fx.SPEED_PERIODS
    normal = periods.get("normal")
    this = periods.get(speed)
    if not normal or not this:
        return 1.0
    return float(normal) / float(this)


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

        # Per-channel runtime state for RGB lighting.  ``_lighting_cfg`` mirrors the
        # last-applied LightingConfig so reconnects re-apply the latest settings.
        self._lighting: dict[str, _LightingState] = {
            channel: _LightingState() for channel in _LIGHTING_CHANNELS
        }
        self._lighting_cfg: LightingConfig = config.lighting

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
        # When set, the initial LCD apply (and sensor streaming) is deferred until
        # this monotonic deadline. Used only at boot, so OpenKraken's fragile
        # multi-step LCD bucket writes don't race the boot HID/USB storm (winedevice,
        # OpenRGB scan, etc.) which intermittently corrupts the panel to black.
        # The firmware screen shows during the wait; cooling/lighting apply at once.
        self._lcd_apply_pending_until: float | None = None
        # Boot cooling re-apply: a curve written during the early-boot HID storm is
        # accepted (the write returns ok) but the firmware doesn't honour it -- the
        # pump/fan run their firmware default until re-applied. Cooling is written
        # once (firmware runs it autonomously), so unlike the streamed LCD it is
        # never re-sent. When starting during boot we therefore re-apply the curves
        # once more after this monotonic deadline (the same grace as the LCD).
        self._cooling_reapply_pending_until: float | None = None
        # LCD self-heal: monotonic time sensor streaming (re)started this connect,
        # and the last forced re-assert. Used to periodically re-establish image
        # display mode so a silently-blacked panel recovers (the panel can't be
        # read back to detect black; see _tick_lcd_selfheal). ``None`` => not
        # streaming sensors right now (fresh/disconnected/other mode).
        self._lcd_stream_started_at: float | None = None
        self._last_lcd_reassert: float = 0.0
        # One-shot flag so "LCD unavailable (bulk)" is surfaced once per episode
        # instead of every 2 s tick (see _tick_lcd_sensors / issue #1).
        self._lcd_bulk_notified: bool = False
        # Re-assert window (monotonic deadline): after an LCD content change the
        # firmware can repaint the LED ring to its default, so for a few seconds
        # we re-stream the app's lighting each tick to keep the user's settings.
        self._lighting_reassert_until: float = 0.0
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
            ring_color=tuple(cfg.ring_color),
        )
        self._requests.put(lambda: self._do_apply_lcd(snapshot))

    def clear_lcd_media(self) -> None:
        """Request erasure of the media stored in the cooler's LCD memory.

        Thread-safe: enqueues the work onto the engine loop.  After clearing,
        the currently configured LCD mode is re-applied so the screen never
        stays on the bare firmware fallback.
        """
        self._requests.put(self._do_clear_lcd_media)

    def _do_clear_lcd_media(self) -> None:
        """Engine-thread worker for :meth:`clear_lcd_media`."""
        if self._device.clear_lcd_media():
            self.applied.emit(
                "lcd", "stored media cleared (boot screen reset to firmware default)"
            )
            # _delete_all_buckets leaves the screen in liquid mode; re-apply the
            # configured mode in case the user runs a sensors/static/gif screen.
            # (Engine-thread context, so using the live mirror is safe.)
            self._do_apply_lcd(self._lcd_cfg)
        else:
            self.error.emit("Could not clear the cooler's stored media")

    def apply_lighting(self, cfg: LightingConfig) -> None:
        """Request that RGB lighting be (re)configured from ``cfg``.

        Thread-safe: enqueues the work onto the engine loop.  A deep snapshot of
        ``cfg`` is taken so later GUI mutation cannot race the engine thread.
        """
        snapshot = LightingConfig(
            enabled=cfg.enabled,
            sync=cfg.sync,
            ring=_copy_lighting_channel(cfg.ring),
            fans=_copy_lighting_channel(cfg.fans),
        )
        self._requests.put(lambda: self._do_apply_lighting(snapshot))

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

                # 4. Fire a deferred boot-time LCD apply once its grace elapses,
                #    then push an LCD sensor frame if due.
                self._tick_deferred_cooling()
                self._tick_deferred_lcd()
                self._tick_lcd_selfheal()
                self._tick_lcd_sensors(status, snap)

                # 5. Stream an animated lighting frame if due (~1 FPS ceiling).
                self._tick_lighting()

                # 6. Drain queued GUI requests.
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
            # Defer the LCD apply only when we're starting *during boot* (small
            # system uptime), so the boot HID/USB storm can settle first. A manual
            # restart later applies immediately (so it recovers a black screen now).
            self._apply_all_configs(defer_lcd=self._in_boot_window())

    def _in_boot_window(self) -> bool:
        """True if the system booted recently and an LCD grace delay should apply."""
        grace = float(getattr(self._config, "lcd_startup_grace_s", 0.0) or 0.0)
        if grace <= 0.0:
            return False
        try:
            with open("/proc/uptime", encoding="ascii") as fh:
                uptime = float(fh.read().split()[0])
        except (OSError, ValueError):
            return False
        return uptime < _LCD_GRACE_BOOT_WINDOW

    def _apply_all_configs(self, defer_lcd: bool = False) -> None:
        """(Re)apply pump, fan and LCD configs from the current AppConfig.

        Called on startup (when ``apply_on_start``) and automatically after every
        successful (re)connection.  When *defer_lcd* is set, cooling and lighting
        are applied immediately but the LCD apply is postponed (the run loop fires
        it once :attr:`_lcd_apply_pending_until` elapses) to dodge boot contention.
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
        # Apply lighting BEFORE the LCD: the LCD content write disturbs the LED
        # ring, and _do_apply_lcd's repaint then runs with the freshly-loaded
        # lighting state (rather than a stale/default one). Only touch the LEDs
        # when the user has enabled control (INTERFACES-LIGHTING.md).
        if self._lighting_cfg.enabled:
            self._do_apply_lighting(self._lighting_cfg)
        if defer_lcd:
            grace = float(getattr(self._config, "lcd_startup_grace_s", 0.0) or 0.0)
            self._lcd_apply_pending_until = time.monotonic() + grace
            # The immediate cooling write above may not have stuck (boot HID storm);
            # re-apply the curves once after the same grace so a dropped boot write
            # doesn't leave the pump/fan on the firmware default until a manual apply.
            self._cooling_reapply_pending_until = time.monotonic() + grace
            _LOGGER.info(
                "deferring LCD apply %.0fs (boot grace) to avoid early HID contention; "
                "cooling will re-apply after the same grace",
                grace,
            )
        else:
            self._lcd_apply_pending_until = None
            self._cooling_reapply_pending_until = None
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

    def _tick_deferred_lcd(self) -> None:
        """Fire the boot-deferred LCD apply once its grace period has elapsed."""
        if self._lcd_apply_pending_until is None:
            return
        if time.monotonic() < self._lcd_apply_pending_until:
            return
        self._lcd_apply_pending_until = None
        if self._device.is_connected:
            _LOGGER.info("boot grace elapsed; applying LCD now")
            self._do_apply_lcd(self._lcd_cfg)

    def _tick_deferred_cooling(self) -> None:
        """Re-apply pump/fan curves once after the boot grace.

        A curve written during the early-boot HID storm can be accepted yet not
        honoured by the firmware (the pump/fan keep their default until re-applied).
        Re-asserting the configured curves once the grace has elapsed makes the
        boot-time setting stick without a manual apply.  Idempotent.
        """
        if self._cooling_reapply_pending_until is None:
            return
        if time.monotonic() < self._cooling_reapply_pending_until:
            return
        self._cooling_reapply_pending_until = None
        if self._device.is_connected:
            _LOGGER.info("boot grace elapsed; re-applying cooling curves")
            self._do_apply_channel("pump", self._config.pump)
            self._do_apply_channel("fan", self._config.fan)

    # ------------------------------------------------------------------ #
    # Loop step 4: LCD sensor frames.
    # ------------------------------------------------------------------ #
    def _tick_lcd_selfheal(self) -> None:
        """Periodically re-assert the sensor screen so a blacked panel recovers.

        The Kraken LCD can't be read back, so we can't *detect* a black panel; a
        bucket switch can silently report success on an HID reply de-sync, leaving
        the double-buffer pointing at a bucket the firmware never displayed (and a
        later frame deletes the one actually on screen -> black, nothing logged).
        We therefore re-establish image display mode unconditionally on a schedule:
        a fast cadence for the first ``lcd_selfheal_boot_phase_s`` of streaming
        (when the boot HID storm makes de-syncs likeliest), then a slow steady-state
        interval.  Each re-assert is one full image-mode frame (a brief refresh of
        the same content), routed via :meth:`KrakenDevice.request_lcd_reinit`.
        """
        # Only meaningful while actively streaming the sensor screen.
        if (
            self._lcd_apply_pending_until is not None
            or self._lcd_cfg.mode != "sensors"
            or not self._device.is_connected
        ):
            self._lcd_stream_started_at = None
            return

        now = time.monotonic()
        if self._lcd_stream_started_at is None:
            # Streaming (re)started: the boot apply / mode switch already
            # established image mode, so don't re-assert immediately -- just arm
            # the timer from here.
            self._lcd_stream_started_at = now
            self._last_lcd_reassert = now
            return

        boot_phase = float(getattr(self._config, "lcd_selfheal_boot_phase_s", 0.0) or 0.0)
        in_boot_phase = (now - self._lcd_stream_started_at) < boot_phase
        if in_boot_phase:
            interval = float(getattr(self._config, "lcd_selfheal_boot_interval_s", 0.0) or 0.0)
        else:
            interval = float(getattr(self._config, "lcd_selfheal_interval_s", 0.0) or 0.0)
        if interval <= 0.0:  # cadence disabled for this phase
            return
        if now - self._last_lcd_reassert < interval:
            return
        self._last_lcd_reassert = now
        _LOGGER.info(
            "LCD self-heal: re-asserting sensor screen (%s cadence)",
            "boot" if in_boot_phase else "steady",
        )
        self._device.request_lcd_reinit()

    def _tick_lcd_sensors(self, status: DeviceStatus, snap: SystemSnapshot) -> None:
        """Render and push an LCD sensor frame if in "sensors" mode and due."""
        # Hold off streaming while the boot-time LCD apply is still deferred (the
        # firmware screen shows meanwhile).
        if self._lcd_apply_pending_until is not None:
            return
        if self._lcd_cfg.mode != "sensors":
            return
        # LCD bulk interface latched off (EBUSY/EACCES, e.g. the "(broken)" 2023
        # Elite 0x300C): sensor frames cannot reach the panel, but the device is
        # otherwise fine.  Surface it once and stop rendering/pushing -- do NOT
        # disconnect (that caused an infinite reconnect storm, issue #1).
        if getattr(self._device, "lcd_bulk_unavailable", False):
            if not self._lcd_bulk_notified:
                self._lcd_bulk_notified = True
                _LOGGER.warning(
                    "LCD image uploads unavailable (USB bulk interface busy or "
                    "inaccessible); sensor screen disabled, cooling/lighting "
                    "unaffected. The firmware screen keeps running."
                )
                self.error.emit(
                    "LCD unavailable: the cooler's USB bulk interface is busy or "
                    "inaccessible, so image/sensor screens can't be uploaded. "
                    "Cooling and lighting still work."
                )
            return
        self._lcd_bulk_notified = False
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
            cpu_vendor=self._sensors.cpu_vendor,
            gpu_vendor=self._sensors.gpu_vendor,
            ring_color=tuple(self._lcd_cfg.ring_color),
        )
        try:
            path = lcd_render.render_to_file(self._lcd_cfg.sensor_style, data)
        except Exception:  # pragma: no cover - rendering must not crash the loop
            _LOGGER.exception("failed to render LCD sensor frame")
            return
        # Double-buffered upload: flicker-free streaming (holds the last frame on
        # a failed push) vs the one-shot set_lcd_static used elsewhere.
        if not self._device.set_lcd_sensor_frame(path):
            _LOGGER.debug("LCD sensor frame not pushed (held previous frame)")
            self._emit_connection(self._device.is_connected)
            return
        # The LCD upload disturbs the LED ring; repaint it (no-op if lighting off).
        self._repaint_lighting()

    # ------------------------------------------------------------------ #
    # Loop step 5: RGB lighting frame streaming.
    # ------------------------------------------------------------------ #
    def _tick_lighting(self) -> None:
        """Stream one animated lighting frame per due channel (~1 FPS ceiling).

        Non-animated modes (off/fixed) were written once at apply time and are not
        touched here.  Animated channels write a fresh :func:`lighting_fx.frame` at
        most once per :data:`_LIGHTING_MIN_WRITE_INTERVAL`.  Nothing is written when
        lighting is disabled or the device is disconnected (PROTOCOL.md §5 /
        INTERFACES-LIGHTING.md).
        """
        if not self._lighting_cfg.enabled:
            return
        if not self._device.is_connected:
            return
        now = time.monotonic()
        # During the re-assert window after an LCD content change, re-stream every
        # channel (incl. fixed ones) so the firmware can't leave the ring on its
        # default — this keeps the app's lighting when switching to the firmware
        # liquid screen (item: "keep the lighting settings, don't reset").
        if now < self._lighting_reassert_until:
            for channel, state in self._lighting.items():
                self._write_lighting_frame(channel, state, now)
            return
        for channel, state in self._lighting.items():
            if not state.animated:
                continue
            if now - state.last_write < self._lighting_write_interval():
                continue
            # _write_lighting_frame advances state.last_write on success, so a
            # failed write does not consume the slot and the next tick can retry.
            self._write_lighting_frame(channel, state, now)

    def _lighting_write_interval(self) -> float:
        """Seconds between animated lighting frames, from ``config.lighting_fps``.

        Clamped to :data:`_LIGHTING_FPS_MIN`..:data:`_LIGHTING_FPS_MAX`; falls
        back to :data:`_LIGHTING_MIN_WRITE_INTERVAL` when unset/invalid.  Higher
        FPS is what makes breathing/cycle continuous instead of stepping once a
        second (5 FPS soak-tested on the 2024 Elite RGB with zero failures).
        """
        fps = float(getattr(self._config, "lighting_fps", 0.0) or 0.0)
        if fps <= 0.0:
            return _LIGHTING_MIN_WRITE_INTERVAL
        return 1.0 / max(_LIGHTING_FPS_MIN, min(_LIGHTING_FPS_MAX, fps))

    def _repaint_lighting(self) -> None:
        """Re-send the current frame for every channel after an LCD write.

        An LCD content upload (sensor refresh, static image, GIF) shares the HID
        interface with the ring/fan LED controller and disturbs it: a few LEDs --
        in practice the ones carried by the ``0x22 0x11`` continuation packet
        (ring LEDs 20-23, the top-left arc) -- drop back to the firmware default
        (green).  Re-streaming the current frame immediately after the LCD write
        repaints them.  Cheap (a few 64-byte reports) and only runs when lighting
        is enabled and the device is connected.  Same root interaction OpenRGB
        hits on this device; see PROTOCOL.md Quirk A / §11.
        """
        if not self._lighting_cfg.enabled or not self._device.is_connected:
            return
        now = time.monotonic()
        for channel, state in self._lighting.items():
            self._write_lighting_frame(channel, state, now)
        # Keep re-asserting for a few seconds: the firmware can repaint the ring
        # to its default shortly after an LCD content/mode change (esp. the
        # switch to the firmware liquid screen).
        self._lighting_reassert_until = now + 3.0

    def _write_lighting_frame(
        self, channel: str, state: _LightingState, now: float
    ) -> bool:
        """Compute and write one frame for ``channel`` from ``state`` at time ``now``.

        Returns ``True`` on a successful device write.  Frame computation failures
        are caught (they must never crash the loop); a write failure reflects any
        resulting disconnect transition, mirroring the other loop steps.
        """
        led_count = self._lighting_led_count(channel)
        # ``lighting_fx.frame`` takes no speed argument: it advances effects on a
        # "normal"-speed time axis.  We encode the per-channel speed by warping the
        # elapsed time so a "fast" channel advances its phase proportionally faster
        # (and "slow" proportionally slower) -- SPEED_PERIODS maps speed -> seconds
        # per cycle, so the warp factor is normal_period / this_speed_period.
        elapsed = max(0.0, now - state.origin) * _speed_time_warp(state.speed)
        colors = state.colors if state.colors is not None else []
        try:
            frame = lighting_fx.frame(
                state.mode, colors, state.brightness, led_count, elapsed
            )
        except Exception:  # pragma: no cover - effect math must not crash the loop
            _LOGGER.exception("failed to compute lighting frame for %s", channel)
            return False
        if not self._device.write_lighting_frame(channel, frame):
            _LOGGER.warning("failed to write lighting frame for %s", channel)
            self._emit_connection(self._device.is_connected)
            return False
        # Record the time of this successful write so the next streamed frame is
        # spaced a full _LIGHTING_MIN_WRITE_INTERVAL after it.  This covers BOTH
        # the apply-time first frame (_do_apply_lighting) and steady-state ticks;
        # without it the apply-time frame leaves last_write at its 0.0 sentinel and
        # the very next tick would fire immediately (<1 s later when poll_interval
        # < 1 s), breaching the device's ~1 FPS ceiling (PROTOCOL.md §5).
        state.last_write = now
        return True

    def _lighting_led_count(self, channel: str) -> int:
        """LED count for ``channel`` from ``device.lighting_info`` or the fallback.

        Falls back to ring=24 / fans=16 (PROTOCOL.md §2) until the device has
        successfully parsed its ``0x20 0x03`` accessory reply.
        """
        info = getattr(self._device, "lighting_info", None)
        if info is not None:
            count = info.led_counts.get(channel)
            if isinstance(count, int) and count > 0:
                return count
        return _LIGHTING_FALLBACK_LEDS.get(channel, 16)

    # ------------------------------------------------------------------ #
    # Loop step 6: request draining.
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
        # Uploading LCD content (static/gif, and the liquid-mode bucket switch)
        # disturbs the LED ring, so repaint lighting afterwards (no-op if off).
        if cfg.mode == "liquid":
            ok = self._device.set_lcd_liquid_mode()
            self._emit_apply_result("lcd", "liquid temperature screen", ok)
            self._repaint_lighting()
        elif cfg.mode == "static":
            if cfg.image_path:
                ok = self._device.set_lcd_static(cfg.image_path)
                self._emit_apply_result("lcd", f"static image {cfg.image_path}", ok)
                self._repaint_lighting()
            else:
                _LOGGER.warning("LCD static mode requested without an image path")
                self.error.emit("LCD: no static image selected")
        elif cfg.mode == "gif":
            if cfg.gif_path:
                ok = self._device.set_lcd_gif(cfg.gif_path)
                self._emit_apply_result("lcd", f"animated GIF {cfg.gif_path}", ok)
                self._repaint_lighting()
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

    def _do_apply_lighting(self, cfg: LightingConfig) -> None:
        """Apply an RGB lighting config on the engine thread.

        * When ``cfg.sync`` the ``ring`` channel config drives **both** channels;
          otherwise each channel uses its own config.
        * Each channel's runtime state is refreshed (mode/colors/brightness/speed)
          and its animation origin reset to ``time.monotonic()`` so animated effects
          restart cleanly on every (re)apply.
        * Non-animated modes (off/fixed) are written **once**, now.  Animated modes
          write their first frame now and are then streamed by :meth:`_tick_lighting`
          at the ~1 FPS device ceiling.
        * Nothing is written while lighting is disabled or the device is
          disconnected; the captured state is still kept so the loop / a later
          reconnect can resume from it.
        """
        # Mirror into the engine's view so reconnect re-applies the latest request.
        self._lighting_cfg = cfg
        self._config.lighting = cfg

        now = time.monotonic()
        for channel in _LIGHTING_CHANNELS:
            source = cfg.ring if cfg.sync else getattr(cfg, channel)
            self._load_lighting_state(channel, source, now)

        if not cfg.enabled:
            # User has disabled control: never touch the LEDs.  We still recorded
            # the desired state above so re-enabling (or a future reconnect with the
            # config enabled) starts from the right place.
            _LOGGER.info("lighting disabled; not writing LEDs")
            self.applied.emit("lighting", "disabled")
            return

        if not self._device.is_connected:
            _LOGGER.debug("apply_lighting deferred: device disconnected")
            return

        # Write the initial frame for every channel (animated channels are then
        # streamed by the loop; static channels are done after this single write).
        details: list[str] = []
        all_ok = True
        for channel in _LIGHTING_CHANNELS:
            state = self._lighting[channel]
            ok = self._write_lighting_frame(channel, state, now)
            all_ok = all_ok and ok
            details.append(f"{channel}: {state.mode}")

        detail = ", ".join(details)
        if cfg.sync:
            detail = f"sync {self._lighting['ring'].mode} ({detail})"
        self._emit_apply_result("lighting", detail, all_ok)

    def _load_lighting_state(
        self, channel: str, source: LightingChannelConfig, now: float
    ) -> None:
        """Refresh ``channel``'s runtime lighting state from ``source``.

        Resets the animation origin to ``now`` and arms the next animated write so
        the first frame is emitted immediately by the caller / next tick.
        """
        spec = lighting_fx.MODES.get(source.mode)
        animated = bool(spec.animated) if spec is not None else False
        state = self._lighting[channel]
        state.mode = source.mode
        state.colors = [tuple(c) for c in source.colors]
        state.brightness = source.brightness
        state.speed = source.speed
        state.animated = animated
        state.origin = now
        # Arm the next animated write with a past sentinel so that if the apply
        # path is deferred (device disconnected) the first tick after a reconnect
        # fires immediately.  When the apply path DOES write the first frame,
        # _write_lighting_frame overwrites this with the real write time, so the
        # next streamed frame is spaced a full _LIGHTING_MIN_WRITE_INTERVAL later
        # (the device's ~1 FPS ceiling, PROTOCOL.md §5) rather than ~poll_interval.
        state.last_write = 0.0

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
