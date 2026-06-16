"""Thread-safe wrapper around the liquidctl Kraken Z3 driver.

This is the single choke-point for *all* device I/O in OpenKraken.  Every public
method serializes driver access through an internal :class:`threading.RLock`, so
the engine thread, startup, and shutdown can all touch the device safely.

Design notes
------------
* ``liquidctl`` is imported lazily (inside :meth:`KrakenDevice.connect`) so that
  merely importing this module never enumerates USB devices or touches
  ``/dev/hidraw*``.  Only the driver class :class:`KrakenZ3` is referenced, and
  only for ``isinstance`` matching.
* The device on the target machine (``1e71:3012`` "NZXT Kraken 2024 Elite RGB")
  is matched by driver class ``KrakenZ3`` and vendor id ``0x1e71``.
* Status / initialize tuple lists are parsed *by label substring* (case
  insensitive), never by index, because the driver returns them sorted and the
  order is not guaranteed.
* Any driver exception (``OSError``, ``ValueError``, assertion failures, USB
  errors, ...) is caught, logged, and turned into a failure value.  On I/O-style
  errors the device is marked disconnected so the engine can attempt to
  reconnect.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Name of the liquidctl driver logger that emits the (otherwise swallowed)
#: LCD bucket-transfer failures we recover from.
_DRIVER_LOGGER_NAME = "liquidctl.driver.kraken3"

#: Total LCD bucket memory (in 1024-byte units), mirroring the driver's
#: ``_LCD_TOTAL_MEMORY``; divided into :data:`_LCD_RING_BUCKETS` slots.
_LCD_TOTAL_MEMORY = 24320

#: Number of LCD buckets the sensor-frame streamer rotates through. Two would be
#: the minimum for flicker-free double-buffering, but a silent HID switch de-sync
#: (the driver's ``_switch_bucket`` can falsely report success on a stale reply)
#: then leaves our "live bucket" pointer one ahead of the firmware's -- and with
#: only two slots the very next frame reuses the bucket actually on screen ->
#: black, nothing logged. Rotating through more buckets and always reusing the
#: OLDEST one means such a de-sync only shows a one-frame-stale image instead of
#: blanking; it self-corrects on the next good switch. The device has 16 buckets
#: and ample memory (each slot here is _LCD_TOTAL_MEMORY/N >> a frame), so 6 is
#: cheap and tolerates a burst of up to N-2 = 4 consecutive de-syncs before the
#: reuse pointer could reach the displayed bucket (the self-heal covers rarer
#: bursts). See :meth:`set_lcd_sensor_frame`.
_LCD_RING_BUCKETS = 6

#: Per-slot memory size (in 1024-byte units) for the bucket ring.  Slots are
#: PACKED at ``slot_index * _LCD_BUCKET_SLOT`` near offset 0 -- the firmware
#: rejects ``_setup_bucket`` at large memory offsets, so the slot must be only a
#: generous bound on one rendered frame (observed ~28), NOT memory/N.  Frames
#: larger than this fall back to the driver's static path.  N * slot must stay
#: well under :data:`_LCD_TOTAL_MEMORY` (6 * 128 = 768 << 24320).
_LCD_BUCKET_SLOT = 128

#: Plain (no-clear) re-upload attempts for transient HID-contention failures
#: before resorting to a bucket clear, and total clear+retry attempts after that.
#: Kept small: each plain retry consumes a fresh bucket, so over-retrying just
#: hastens memory exhaustion (and its clear). The switch guard already keeps a
#: failed frame off-screen, so a skipped frame merely holds the last good image.
_LCD_PLAIN_RETRIES = 2
_LCD_CLEAR_RETRIES = 2


class _BucketFailureWatcher(logging.Handler):
    """Detects the driver's swallowed LCD bucket-transfer failures.

    The kraken3 driver only *logs* "Failed to setup bucket for data transfer"
    (and "Failed to switch active bucket") and then returns as if the upload
    succeeded -- leaving an empty/garbage bucket on screen.  This handler flips a
    flag the device wrapper checks after each upload so it can clear the buckets
    and retry (see :meth:`KrakenDevice._set_screen`).  The device's bucket memory
    fills after ~30 same-size frames and the driver's own exhaustion-reset is
    unreliable on the 2024 Elite, so sensor mode goes permanently dark without
    this recovery.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.failed = False

    def reset(self) -> None:
        self.failed = False

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Failed to setup bucket" in msg or "Failed to switch active bucket" in msg:
            self.failed = True


#: Vendor id for NZXT (all Kraken devices).
_NZXT_VENDOR_ID = 0x1E71

# Exceptions the liquidctl driver can raise from ``set_screen`` that are
# *recoverable* content/validation problems on a healthy, connected device (an
# oversized GIF, a missing/non-image path, an out-of-range value).  These must NOT
# mark the device disconnected (which would cause a spurious reconnect storm), so
# they are caught and turned into a plain ``False`` return.  Pillow's
# ``UnidentifiedImageError`` (raised by ``Image.open`` on a non-image) is added
# when Pillow is importable; it subclasses ``OSError`` but is a validation error,
# not a device-I/O error, so it is matched here *before* the broad ``Exception``
# handler that does disconnect.
_RECOVERABLE_SCREEN_ERRORS: tuple[type[BaseException], ...] = (
    AssertionError,
    FileNotFoundError,
    ValueError,
)
try:  # pragma: no cover - Pillow is a backend dependency but guard anyway
    from PIL import UnidentifiedImageError as _UnidentifiedImageError

    _RECOVERABLE_SCREEN_ERRORS = _RECOVERABLE_SCREEN_ERRORS + (_UnidentifiedImageError,)
except Exception:  # pragma: no cover - Pillow missing/old
    pass

# Status / initialize label fragments (lower-cased substring matches).
_LBL_LIQUID_TEMP = "liquid temperature"
_LBL_PUMP_SPEED = "pump speed"
_LBL_PUMP_DUTY = "pump duty"
_LBL_FAN_SPEED = "fan speed"
_LBL_FAN_DUTY = "fan duty"
_LBL_FIRMWARE = "firmware"
_LBL_LCD_BRIGHTNESS = "brightness"
_LBL_LCD_ORIENTATION = "orientation"

# --------------------------------------------------------------------------- #
# Native LED control (NZXT HUE2 "Direct" path).  Every byte layout below is
# taken from the confirmed sections of PROTOCOL.md (§2 channel map, §3 init,
# §4 direct write, §6 discovery) -- NOTHING from §7 (do-not-use) or §9 (FYI
# hardware-effect tables).  All of it is encapsulated here; nothing byte-level
# leaks above device.py.
# --------------------------------------------------------------------------- #

#: App lighting channel name -> wire mask written into HID byte ``0x02``
#: (PROTOCOL.md §2: ring = channel 0 = mask ``1<<0``, fans = channel 1 = ``1<<1``).
LIGHTING_CHANNELS: dict[str, int] = {"ring": 0x01, "fans": 0x02}

#: HID report length for the lighting (HUE2) path; right-zero-padded (PROTOCOL.md
#: §3 / kraken3.py ``_WRITE_LENGTH``/``_READ_LENGTH``).
_LIGHTING_REPORT_LENGTH = 64

#: Reads to attempt while waiting for the ``0x21 0x03`` lighting-info reply
#: (mirrors kraken3.py ``_MAX_READ_ATTEMPTS``).
_LIGHTING_MAX_READ_ATTEMPTS = 12

#: Bytes per LED on the wire: one G, R, B triplet (PROTOCOL.md §4).
_BYTES_PER_LED = 3

#: First report (``0x22 0x10``) carries up to 20 LEDs = 60 colour bytes; the rest
#: spill into the ``0x22 0x11`` continuation (PROTOCOL.md §4, Quirk A workaround).
_LEDS_PER_PACKET = 20
_COLOR_BYTES_PER_PACKET = _LEDS_PER_PACKET * _BYTES_PER_LED  # 60

#: Discovery: ``0x20 0x03`` request and its ``0x21 0x03`` reply prefix
#: (PROTOCOL.md §3 step 2 / §6).
_LIGHTING_INFO_REQUEST = [0x20, 0x03]
_LIGHTING_INFO_REPLY_PREFIX = (0x21, 0x03)

#: ``0x20 0x03`` reply layout (PROTOCOL.md §6): channel count at byte 14,
#: accessory ids start at byte 15 with a stride of 6 slots per channel.
_LIGHTING_INFO_CHANNEL_COUNT_OFFSET = 14
_LIGHTING_INFO_ACCESSORY_OFFSET = 15
_HUE2_MAX_ACCESSORIES_IN_CHANNEL = 6

#: Accessory id -> LED count, using the OpenRGB-authoritative counts called out
#: in PROTOCOL.md §6 (the installed liquidctl enum does NOT name 0x1E / 0x18, so
#: we keep our own table rather than relying on it).  Unknown ids fall back to
#: ``_FALLBACK_ACCESSORY_LEDS`` with a log line.
_ACCESSORY_LED_COUNTS: dict[int, int] = {
    0x1E: 24,  # Kraken 2024 Elite Pump Ring
    0x17: 8,   # F140 RGB Core (per-fan)
    0x18: 8,   # F120 RGB Core (per-fan)
    0x1B: 24,  # RGB Core radiator-kit aggregate (HW-CONFIRMED 2026-06-02: our unit
    #            reports a single 0x1B on the fan channel; 24-LED frames light the
    #            entire chain — see PROTOCOL.md §11)
    0x1D: 24,  # F360 RGB Core (radiator-kit aggregate)
}
_FALLBACK_ACCESSORY_LEDS = 8

#: Fallback LED counts when ``query_lighting_info()`` never succeeded
#: (PROTOCOL.md §11: our unit reports ring 24 / fans 24).
_FALLBACK_LED_COUNTS: dict[str, int] = {"ring": 24, "fans": 24}

#: Apply-packet (``0x22 0xA0``) byte-7 variants (PROTOCOL.md §4 + §11):
#: index 0 = OpenRGB (0x28) — HW-CONFIRMED on our unit 2026-06-02 (full ring and
#: fan chain lit first try; variant 1 never needed).  Index 1 = liquidctl
#: super-fixed (0x08), kept only for the probe's A/B path.  The only difference
#: between the two 16-byte templates is this byte.
_APPLY_VARIANTS: tuple[tuple[int, ...], ...] = (
    # OpenRGB SendApply (verbatim): byte 0x07 = 0x28.  DEFAULT.
    (0x22, 0xA0, 0x00, 0x00, 0x01, 0x00, 0x00, 0x28, 0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01),
    # liquidctl super-fixed apply: byte 0x07 = 0x08.
    (0x22, 0xA0, 0x00, 0x00, 0x01, 0x00, 0x00, 0x08, 0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01),
)
#: Byte index of the channel mask inside an apply template.
_APPLY_MASK_OFFSET = 2


@dataclass
class LightingInfo:
    """Parsed ``0x20 0x03`` lighting/accessory reply (PROTOCOL.md §6).

    ``accessories`` maps each app channel name to the list of non-zero accessory
    ids found in that channel's slots; ``led_counts`` maps each channel name to
    the summed LED count (ring is expected to report 24).
    """

    channel_count: int
    accessories: dict[str, list[int]]
    led_counts: dict[str, int]


@dataclass
class DeviceStatus:
    """A single snapshot of the cooler's reported telemetry.

    ``connected`` is ``False`` (and all readings ``None``) whenever the most
    recent status read failed or the device is not connected.
    """

    liquid_temp: float | None
    pump_rpm: int | None
    pump_duty: int | None
    fan_rpm: int | None
    fan_duty: int | None
    connected: bool
    timestamp: float  # time.monotonic()

    @classmethod
    def disconnected(cls) -> "DeviceStatus":
        """Return an all-``None`` snapshot flagged as disconnected."""
        return cls(
            liquid_temp=None,
            pump_rpm=None,
            pump_duty=None,
            fan_rpm=None,
            fan_duty=None,
            connected=False,
            timestamp=time.monotonic(),
        )


class KrakenDevice:
    """Thread-safe facade over a single NZXT Kraken Z3-class cooler."""

    # Speed channel duty clamps (mirrors the firmware/driver limits).
    PUMP_DUTY_MIN, PUMP_DUTY_MAX = 20, 100
    FAN_DUTY_MIN, FAN_DUTY_MAX = 0, 100

    #: Liquid temperature at/above which profiles are normalized to 100 % duty.
    CRITICAL_TEMP = 59

    #: Round LCD resolution (px).
    LCD_RESOLUTION = (640, 640)

    def __init__(self) -> None:
        """Construct the wrapper.  Does **not** touch hardware."""
        self._lock = threading.RLock()
        self._dev: Any | None = None
        self._connected: bool = False
        self._description: str = ""
        self.firmware_version: str = ""
        self.lcd_brightness: int = 50
        self.lcd_orientation: int = 0
        #: Parsed lighting/accessory info; ``None`` until a successful
        #: :meth:`query_lighting_info` (which is best-effort during connect()).
        self.lighting_info: LightingInfo | None = None
        #: Raw bytes of the most recent ``0x21 0x03`` lighting-info reply, kept
        #: verbatim so the hardware probe can hex-dump it for PROTOCOL.md §10.3
        #: (no raw dump from a real 3012 exists yet).  ``None`` until a reply with
        #: the ``0x21 0x03`` prefix has been read.
        self.last_lighting_reply: list[int] | None = None
        #: Watches the driver logger for swallowed LCD bucket failures so uploads
        #: can self-heal (installed once, lazily, in :meth:`_set_screen`).
        self._bucket_watcher: _BucketFailureWatcher | None = None
        #: While True (set only around image/GIF uploads), the driver's LCD
        #: bucket-switch is gated so a contention-failed frame never reaches the
        #: panel: the firmware-liquid switch the driver uses for its memory reset
        #: is suppressed, and switching to a bucket whose setup failed is refused
        #: (the panel holds the last good frame). See :meth:`_install_lcd_switch_guard`.
        self._lcd_guard_active: bool = False
        #: Per-bucket result of the most recent ``_setup_bucket`` while guarding.
        self._lcd_setup_ok: dict[int, bool] = {}
        #: Believed position in the LCD bucket ring (0..``_LCD_RING_BUCKETS``-1) of
        #: the frame last successfully switched to -- i.e. what *should* be on
        #: screen.  ``None`` until the first sensor frame lands.  May drift from the
        #: firmware's actual displayed bucket on a silent switch de-sync; the ring
        #: is sized so that drift never blanks the panel.  See
        #: :meth:`set_lcd_sensor_frame`.
        self._lcd_ring_pos: int | None = None
        #: True when the panel is NOT known to be in image/bucket display mode --
        #: a fresh connect, or after a liquid/static/gif :meth:`_set_screen` left it
        #: in firmware-liquid (or another) display mode.  The lightweight
        #: double-buffer ``_switch_bucket`` can only swap the active bucket; it
        #: cannot re-establish image display mode, so while this is set the next
        #: streamed frame is routed through the driver's full ``set_screen("static")``
        #: path (which does).  Otherwise a liquid->sensors switch leaves the panel
        #: black (empty bucket) with no error.  See :meth:`set_lcd_sensor_frame`.
        self._lcd_stream_needs_reinit: bool = True

    def _ensure_bucket_watcher(self) -> _BucketFailureWatcher:
        """Install (once) and return the driver bucket-failure watcher."""
        if self._bucket_watcher is None:
            self._bucket_watcher = _BucketFailureWatcher()
            logging.getLogger(_DRIVER_LOGGER_NAME).addHandler(self._bucket_watcher)
        return self._bucket_watcher

    def _install_lcd_switch_guard(self, dev: Any) -> None:
        """Wrap the driver's bucket setup/switch so a bad frame never displays.

        The kraken3 image-upload path (``_send_data``) switches the panel to the
        freshly-written bucket even when ``_setup_bucket`` failed (HID contention
        wrote nothing valid) -- which flashes the firmware screen mid-stream. We
        wrap both primitives once: while ``_lcd_guard_active`` (set only around
        image/GIF uploads) the wrapper records each setup result and refuses to
        switch to a *data* bucket whose setup failed, so the panel holds the last
        good frame until a retry lands. The driver's memory-reset liquid switch
        (mode ``0x2``) is deliberately NOT blocked -- it is required to free the
        active bucket during a clear. Outside guarded uploads (e.g. an explicit
        liquid-mode request) both pass straight through. No-op if the private
        primitives are absent.
        """
        if getattr(dev, "_ok_switch_guard_installed", False):
            return
        if not (hasattr(dev, "_setup_bucket") and hasattr(dev, "_switch_bucket")):
            logger.debug("LCD switch guard not installed: driver primitives absent")
            return
        orig_setup = dev._setup_bucket
        orig_switch = dev._switch_bucket

        def guarded_setup(start_index, end_index, mem_start, mem_size):
            ok = orig_setup(start_index, end_index, mem_start, mem_size)
            if self._lcd_guard_active:
                self._lcd_setup_ok[start_index] = bool(ok)
            return ok

        def guarded_switch(bucket_index, mode=0x4):
            # NB: we must NOT suppress the mode 0x2 (liquid) switch -- the driver
            # uses it inside _delete_all_buckets to move the panel off the active
            # data bucket so it can be freed; blocking it leaves memory full and
            # every later upload fails (permanent dark). We only refuse switching
            # to a *data* bucket whose setup failed, so a contention-failed frame
            # holds the last good image instead of flashing on screen.
            if self._lcd_guard_active and mode != 0x2:
                if not self._lcd_setup_ok.get(bucket_index, True):
                    return False
            return orig_switch(bucket_index, mode)

        dev._setup_bucket = guarded_setup
        dev._switch_bucket = guarded_switch
        dev._ok_switch_guard_installed = True
        logger.debug("LCD switch guard installed")

    # --------------------------------------------------------------- discovery
    def connect(self) -> bool:
        """Find, open and initialize the cooler.

        Enumerates liquidctl devices, picks the first NZXT (``0x1e71``)
        :class:`KrakenZ3` instance, opens it, and runs ``initialize()`` to learn
        the firmware version and current LCD brightness/orientation.

        Idempotent: returns ``True`` immediately if already connected.  Returns
        ``False`` (and logs) on any failure, leaving the device disconnected.
        """
        with self._lock:
            if self._connected and self._dev is not None:
                return True

            # Lazy import: importing this module must never touch hardware.
            try:
                from liquidctl import find_liquidctl_devices
                from liquidctl.driver.kraken3 import KrakenZ3
            except Exception:
                logger.exception("liquidctl is not importable; cannot connect")
                self._mark_disconnected()
                return False

            try:
                candidates = list(find_liquidctl_devices())
            except Exception:
                logger.exception("find_liquidctl_devices() failed")
                self._mark_disconnected()
                return False

            chosen = None
            for dev in candidates:
                try:
                    vendor_id = getattr(dev, "vendor_id", None)
                except Exception:  # pragma: no cover - defensive
                    vendor_id = None
                if isinstance(dev, KrakenZ3) and vendor_id == _NZXT_VENDOR_ID:
                    chosen = dev
                    break

            if chosen is None:
                logger.warning(
                    "No NZXT Kraken Z3 device found among %d liquidctl device(s)",
                    len(candidates),
                )
                self._mark_disconnected()
                return False

            try:
                chosen.connect()
            except Exception:
                logger.exception("Failed to connect() to the Kraken device")
                self._safe_disconnect(chosen)
                self._mark_disconnected()
                return False

            try:
                init_status = chosen.initialize()
            except Exception:
                logger.exception("Failed to initialize() the Kraken device")
                self._safe_disconnect(chosen)
                self._mark_disconnected()
                return False

            self._dev = chosen
            self._connected = True
            self._description = str(getattr(chosen, "description", "") or "")
            self._install_lcd_switch_guard(chosen)
            # Fresh device: no ring slot is displayed yet, and the panel is in its
            # firmware-stored display mode -- force a full re-init on the first
            # streamed frame so image display mode is established cleanly.
            self._lcd_ring_pos = None
            self._lcd_stream_needs_reinit = True
            self._apply_init_status(init_status)
            logger.info(
                "Connected to %s (fw=%s, brightness=%d%%, orientation=%d°)",
                self._description or "Kraken device",
                self.firmware_version or "?",
                self.lcd_brightness,
                self.lcd_orientation,
            )

            # Best-effort lighting discovery (PROTOCOL.md §3 step 2 / §6).  A
            # failure here must NOT fail the connection: lighting is optional and
            # callers fall back to the default LED counts.  ``query_lighting_info``
            # re-enters the RLock (re-entrant), never raises, and -- crucially --
            # never tears down the connection on its own I/O error (a genuine
            # disconnect is caught by the next get_status).  We still return the
            # live ``_connected`` flag rather than a bare ``True`` so the result is
            # always honest about the connection state.
            self.query_lighting_info()
            return self._connected

    def disconnect(self) -> None:
        """Disconnect from the device if connected.  Always safe to call."""
        with self._lock:
            self._mark_disconnected()

    # ----------------------------------------------------------- introspection
    @property
    def is_connected(self) -> bool:
        """Whether a device is currently open and usable."""
        with self._lock:
            return self._connected and self._dev is not None

    @property
    def description(self) -> str:
        """Human-readable device description, or ``""`` when disconnected."""
        with self._lock:
            return self._description if self.is_connected else ""

    # -------------------------------------------------------------- telemetry
    def get_status(self) -> DeviceStatus:
        """Read a telemetry snapshot.

        On any driver error the device is marked disconnected and an all-``None``
        :class:`DeviceStatus` (``connected=False``) is returned.
        """
        with self._lock:
            if self._dev is None or not self._connected:
                return DeviceStatus.disconnected()
            try:
                raw = self._dev.get_status()
            except Exception:
                logger.exception("get_status() failed; marking device disconnected")
                self._mark_disconnected()
                return DeviceStatus.disconnected()

            parsed = _parse_tuples(raw)
            return DeviceStatus(
                liquid_temp=_get_float(parsed, _LBL_LIQUID_TEMP),
                pump_rpm=_get_int(parsed, _LBL_PUMP_SPEED),
                pump_duty=_get_int(parsed, _LBL_PUMP_DUTY),
                fan_rpm=_get_int(parsed, _LBL_FAN_SPEED),
                fan_duty=_get_int(parsed, _LBL_FAN_DUTY),
                connected=True,
                timestamp=time.monotonic(),
            )

    # ----------------------------------------------------------------- cooling
    def set_speed_profile(self, channel: str, points: list[tuple[float, int]]) -> bool:
        """Write a liquid-temp duty profile for ``channel`` (``pump``/``fan``).

        The driver normalizes/interpolates the profile to a 40-point firmware
        curve.  Returns ``True`` on success, ``False`` (logged) on failure; an
        I/O failure marks the device disconnected.
        """
        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("set_speed_profile(%s): device not connected", channel)
                return False
            try:
                self._dev.set_speed_profile(channel, list(points))
                return True
            except Exception:
                logger.exception(
                    "set_speed_profile(%s, %r) failed", channel, points
                )
                self._mark_disconnected()
                return False

    def set_fixed_speed(self, channel: str, duty: int) -> bool:
        """Set ``channel`` to a flat ``duty`` (clamped to the channel limits)."""
        clamped = self._clamp_duty(channel, duty)
        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("set_fixed_speed(%s): device not connected", channel)
                return False
            try:
                self._dev.set_fixed_speed(channel, clamped)
                logger.info("Set %s fixed speed to %d%%", channel, clamped)
                return True
            except Exception:
                logger.exception(
                    "set_fixed_speed(%s, %d) failed", channel, clamped
                )
                self._mark_disconnected()
                return False

    # --------------------------------------------------------------------- LCD
    def set_lcd_brightness(self, value: int) -> bool:
        """Set LCD brightness (0-100 %).  Updates :attr:`lcd_brightness`."""
        clamped = _clamp(int(value), 0, 100)
        if self._set_screen("brightness", clamped):
            with self._lock:
                self.lcd_brightness = clamped
            return True
        return False

    def set_lcd_orientation(self, degrees: int) -> bool:
        """Set LCD orientation (0/90/180/270 °).  Updates :attr:`lcd_orientation`."""
        deg = int(degrees)
        if deg not in (0, 90, 180, 270):
            logger.warning("Invalid LCD orientation %r; ignoring", degrees)
            return False
        if self._set_screen("orientation", deg):
            with self._lock:
                self.lcd_orientation = deg
            return True
        return False

    def request_lcd_reinit(self) -> None:
        """Force the next streamed sensor frame through the full image-mode path.

        Sets the same flag a fresh connect / mode switch sets, so the next
        :meth:`set_lcd_sensor_frame` runs the full liquid->image mode-cycle
        recovery (the automated form of the manual "switch LCD mode and back")
        and resets the double-buffer.  Used by the engine's periodic LCD self-heal
        to recover a silently-blacked panel (an HID reply de-sync can desync the
        double-buffer with no error logged; the panel can't be read back to detect
        it, so the recovery runs unconditionally on a schedule).
        """
        self._lcd_stream_needs_reinit = True

    def set_lcd_liquid_mode(self) -> bool:
        """Switch the LCD to the firmware liquid-temperature screen."""
        ok = self._set_screen("liquid", None)
        # The panel is now in firmware-liquid display mode (and its bucket memory
        # was reset); a later switch back to sensor streaming must re-establish
        # image display mode via the full path, not just a bucket switch.
        self._lcd_stream_needs_reinit = True
        return ok

    def set_lcd_static(self, image_path: str) -> bool:
        """Upload a static image to the LCD (blocking; ~0.1-1 s)."""
        ok = self._set_screen("static", str(image_path))
        # Driver-managed buckets now own the panel; the double-buffer streamer's
        # bookkeeping is stale, so a return to sensors must re-init (see flag).
        self._lcd_stream_needs_reinit = True
        return ok

    def set_lcd_gif(self, gif_path: str) -> bool:
        """Upload an animated GIF to the LCD (blocking)."""
        ok = self._set_screen("gif", str(gif_path))
        self._lcd_stream_needs_reinit = True
        return ok

    #: Names of the private liquidctl primitives the double-buffered sensor
    #: uploader needs; absence (older/newer driver) falls back to set_lcd_static.
    _DB_PRIMITIVES = (
        "_prepare_static_file",
        "_setup_bucket",
        "_write_then_read",
        "_bulk_write",
        "_write",
        "_switch_bucket",
        "_delete_bucket",
    )

    def set_lcd_sensor_frame(self, image_path: str) -> bool:
        """Upload one streamed sensor frame, flicker-free, via a bucket ring.

        Continuous frame streaming through the driver's ``set_screen`` glitches
        the panel: it rotates through 16 buckets (exhausting memory ~every 30
        frames, which forces a clear that flips to the firmware liquid screen),
        and it switches the display to each freshly-allocated bucket even on a
        failed/partial write.  Instead we keep our own ring of
        :data:`_LCD_RING_BUCKETS` fixed-offset buckets and write the OLDEST one
        fully, then switch to it only when every step succeeded.  We never reuse a
        bucket that could still be on screen, so there is no rotation/exhaustion
        (no clears, no liquid flash) and a failed upload simply leaves the previous
        frame up.

        Crucially this also survives a *silent* switch de-sync: the driver's
        ``_switch_bucket`` reads a reply and returns ``response[14] == 0x1``, which
        on an out-of-order/stale reply can be a false positive -- the firmware
        never actually switched, but we think it did.  With only two buckets the
        next frame would then reuse (delete) the bucket actually on screen and the
        panel goes black with nothing logged.  With a ring of N>=3 the bucket we
        reuse is the oldest, never the displayed one (even after several
        consecutive de-syncs), so the worst case is a one-frame-stale image that
        self-corrects on the next good switch -- not a black screen.

        Falls back to :meth:`set_lcd_static` if the driver lacks the private
        primitives this relies on (it pins liquidctl, so they are present).
        Returns ``True`` only when a new frame reached the panel.
        """
        # First frame after a fresh connect, a liquid/static/gif switch, or a
        # self-heal: re-establish the panel's image display mode.  A plain static
        # re-push only does a mode-0x4 bucket switch -- the very op that silently
        # false-positives on an HID reply de-sync, so it CANNOT un-wedge a panel
        # that has silently gone black (proven in the field: repeated static
        # re-pushes left the screen black).  Replicate the manual recovery that
        # DOES work -- "switch to another LCD mode and back": force the firmware
        # into its built-in liquid display mode first (mode 0x2, a deeper reset of
        # the display pipeline + bucket memory), THEN re-establish image mode with
        # the static upload.  ``set_screen("liquid")`` is called directly (not via
        # set_lcd_liquid_mode) so it doesn't re-arm the reinit flag.  Done outside
        # the lock because ``_set_screen`` acquires it itself.
        if self._lcd_stream_needs_reinit:
            self._set_screen("liquid", None)  # deep display-pipeline reset (un-wedge)
            ok = self._set_screen("static", str(image_path))
            if ok:
                with self._lock:
                    self._lcd_ring_pos = None
                    self._lcd_stream_needs_reinit = False
            return ok
        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("set_lcd_sensor_frame: device not connected")
                return False
            dev = self._dev
            if not all(hasattr(dev, n) for n in self._DB_PRIMITIVES) or (
                getattr(dev, "bulk_device", None) is None
            ):
                logger.debug("double-buffer primitives unavailable; using set_lcd_static")
                return self._set_screen("static", str(image_path))

            try:
                data = dev._prepare_static_file(str(image_path), getattr(dev, "orientation", 0))
            except _RECOVERABLE_SCREEN_ERRORS as exc:
                logger.warning("sensor frame: image prepare failed (%s)", exc)
                return False
            except Exception:
                logger.exception("sensor frame: image prepare crashed")
                return False

            # Replicate the driver's static bulk-transfer framing (set_screen
            # "static" -> _send_data with bulkInfo [0x02, 0,0,0] + len32).
            bulk_info = [0x02, 0x0, 0x0, 0x0] + list(len(data).to_bytes(4, "little"))
            header = [0x12, 0xFA, 0x01, 0xE8, 0xAB, 0xCD, 0xEF, 0x98, 0x76, 0x54, 0x32, 0x10] + bulk_info
            data_size = math.ceil((len(header) + len(data)) / 1024)

            # Ring of N fixed, non-overlapping regions. Advance to the next slot
            # (the OLDEST, written N-1 frames ago) so we never reuse the bucket
            # currently on screen -- even if our pointer has drifted from the
            # firmware's after a silent switch de-sync. Slots are packed tightly
            # near offset 0 (slot size = a generous bound on one frame): the
            # firmware rejects bucket setups at large memory offsets, so spreading
            # slots across the full memory makes every non-zero slot fail. Packed
            # small offsets mirror the original adjacent two-buffer layout.
            slot = _LCD_BUCKET_SLOT
            if data_size >= slot:  # frame too big for one ring slot
                logger.debug("frame too large for the bucket ring; using set_lcd_static")
                return self._set_screen("static", str(image_path))
            prev = self._lcd_ring_pos
            target = 0 if prev is None else (prev + 1) % _LCD_RING_BUCKETS
            mem_offset = target * slot

            try:
                dev._write_then_read([0x36, 0x03])  # transfer preamble (per _send_data)
                # Reuse the OLDEST ring slot (target); with N>=3 it is never the
                # displayed one, so the panel keeps its current frame throughout.
                dev._delete_bucket(target)
                if not dev._setup_bucket(
                    target,
                    target + 1,
                    list(mem_offset.to_bytes(2, "little")),
                    list(data_size.to_bytes(2, "little")),
                ):
                    logger.debug("sensor frame: setup bucket %d failed; holding frame", target)
                    return False
                dev._write_then_read([0x36, 0x01, target])  # begin data transfer
                dev._bulk_write(header)
                for i in range(0, len(data), dev.bulk_buffer_size):
                    dev._bulk_write(list(data[i : i + dev.bulk_buffer_size]))
                dev._write([0x36, 0x02])  # end data transfer
                if not dev._switch_bucket(target):
                    logger.debug("sensor frame: switch to bucket %d failed; holding frame", target)
                    return False
                self._lcd_ring_pos = target
                return True
            except Exception:
                logger.exception("sensor frame: bulk transfer failed; marking disconnected")
                self._mark_disconnected()
                return False

    def clear_lcd_media(self) -> bool:
        """Erase all media stored in the cooler's onboard LCD memory.

        Uploaded images/GIFs persist in the device's media buckets and the
        firmware replays the last one standalone (during boot, before any host
        software runs).  This deletes every bucket via the driver's
        ``_delete_all_buckets`` (which first switches the screen to the
        firmware liquid mode), so subsequent boots show the firmware default
        instead of stale media.  Callers should re-apply the configured LCD
        mode afterwards.
        """
        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("clear_lcd_media: device not connected")
                return False
            try:
                # Private driver API (no public equivalent); pinned liquidctl
                # version in the venv makes this dependable.
                self._dev._delete_all_buckets()  # noqa: SLF001
                logger.info("LCD media buckets cleared")
                return True
            except Exception:
                logger.exception("clear_lcd_media failed")
                self._mark_disconnected()
                return False

    # ------------------------------------------------------------ LED lighting
    def query_lighting_info(self) -> LightingInfo | None:
        """Discover per-channel LED accessories (PROTOCOL.md §3 step 2 / §6).

        Writes the ``0x20 0x03`` lighting-info request and reads 64-byte reports
        until one prefixed ``0x21 0x03`` arrives, then parses the channel count
        (byte 14) and the per-channel accessory ids (byte 15 onward, stride 6)
        into a :class:`LightingInfo`.  Each non-zero accessory id is mapped to an
        LED count via :data:`_ACCESSORY_LED_COUNTS` (unknown ids log a warning and
        count as :data:`_FALLBACK_ACCESSORY_LEDS`), summed per channel.

        Stores the result in :attr:`lighting_info` and returns it.  This is
        **best-effort**: any failure (not connected, I/O error, malformed reply)
        is logged and returns ``None`` with :attr:`lighting_info` left unchanged
        from a previous success (or ``None``); callers fall back to
        :data:`_FALLBACK_LED_COUNTS` (ring 24, fans 16).  A genuine I/O error
        marks the device disconnected so the engine can reconnect.
        """
        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("query_lighting_info: device not connected")
                return None
            try:
                # Clear stale reports before the request/reply exchange, exactly
                # as kraken3.py does around its own request reads.
                self._dev.device.clear_enqueued_reports()
                self._lighting_write(_LIGHTING_INFO_REQUEST)
                reply = self._lighting_read_until(_LIGHTING_INFO_REPLY_PREFIX)
            except Exception:
                # Discovery is best-effort and explicitly non-fatal
                # (INTERFACES-LIGHTING.md): an I/O error here must NOT tear down
                # the connection -- doing so would make connect()'s best-effort
                # discovery call report a healthy device as disconnected, and the
                # engine would emit connection_changed(True) then silently skip
                # every apply.  Log it, leave ``lighting_info`` unchanged, and keep
                # the connection; a genuine disconnect is caught by the next
                # control/telemetry call (get_status / write_lighting_frame).
                logger.exception(
                    "query_lighting_info() I/O failed; keeping connection, "
                    "falling back to default LED counts"
                )
                return None

            if reply is None:
                logger.warning(
                    "query_lighting_info: no %02x %02x reply within %d reads; "
                    "falling back to default LED counts",
                    _LIGHTING_INFO_REPLY_PREFIX[0],
                    _LIGHTING_INFO_REPLY_PREFIX[1],
                    _LIGHTING_MAX_READ_ATTEMPTS,
                )
                return None

            # Keep the verbatim wire bytes for the §10.3 hardware-probe hex dump
            # (retained even if parsing below fails, so a malformed reply can
            # still be inspected).
            self.last_lighting_reply = list(reply)

            info = _parse_lighting_info(reply)
            if info is None:
                # Malformed/short reply: validation problem, not an I/O fault --
                # do NOT disconnect.
                return None

            self.lighting_info = info
            logger.info(
                "Lighting info: %d channel(s); LED counts %s; accessories %s",
                info.channel_count,
                info.led_counts,
                info.accessories,
            )
            return info

    def led_count_for(self, channel: str) -> int:
        """Return the detected LED count for ``channel`` (else the fallback).

        Uses :attr:`lighting_info` when available, otherwise the conservative
        defaults from PROTOCOL.md §6 (ring 24, fans 16).
        """
        info = self.lighting_info
        if info is not None and channel in info.led_counts:
            return info.led_counts[channel]
        return _FALLBACK_LED_COUNTS.get(channel, _FALLBACK_ACCESSORY_LEDS)

    def write_lighting_frame(
        self,
        channel: str,
        led_colors: list[tuple[int, int, int]],
        apply_variant: int = 0,
    ) -> bool:
        """Write one full per-LED Direct frame to ``channel`` (PROTOCOL.md §4).

        This is the **only** lighting write path.  ``led_colors`` is a list of
        ``(r, g, b)`` triplets (0-255, already brightness-scaled host-side by the
        effect engine -- the device has no brightness command).  Each triplet is
        converted to the device's **GRB** wire order, then sent as:

        * packet ``0x22 0x10`` carrying LEDs 0-19 (``leds[0:60]``);
        * packet ``0x22 0x11`` carrying LEDs 20-39 (``leds[60:]``) -- sent **only
          when the LED count exceeds 20** (Quirk A workaround: the 24-LED ring
          needs it, a <=20-LED chain does not);
        * the apply/commit packet ``0x22 0xA0`` (``_APPLY_VARIANTS[apply_variant]``,
          default index 0 = OpenRGB byte-7 ``0x28``).

        Returns ``True`` on success, ``False`` (logged) on failure.  An I/O error
        marks the device disconnected so the engine can reconnect.  ``channel``
        must be a key of :data:`LIGHTING_CHANNELS`.
        """
        mask = LIGHTING_CHANNELS.get(channel)
        if mask is None:
            logger.warning(
                "write_lighting_frame: unknown channel %r (valid: %s)",
                channel,
                ", ".join(LIGHTING_CHANNELS),
            )
            return False

        if apply_variant < 0 or apply_variant >= len(_APPLY_VARIANTS):
            logger.warning(
                "write_lighting_frame: apply_variant %r out of range; using 0",
                apply_variant,
            )
            apply_variant = 0

        # RGB -> GRB on the wire (PROTOCOL.md §4), clamped defensively.  Flatten
        # to a single byte list: [g0, r0, b0, g1, r1, b1, ...].
        color_bytes: list[int] = []
        for color in led_colors:
            try:
                r, g, b = color[0], color[1], color[2]
            except (TypeError, IndexError, ValueError):
                logger.warning("write_lighting_frame: skipping malformed colour %r", color)
                continue
            color_bytes.append(_clamp(int(g), 0, 255))
            color_bytes.append(_clamp(int(r), 0, 255))
            color_bytes.append(_clamp(int(b), 0, 255))

        led_count = len(color_bytes) // _BYTES_PER_LED

        # Packet 1: LEDs 0-19 (first 60 colour bytes).  Packet 2 (continuation)
        # is sent ONLY when there are more than 20 LEDs (Quirk A).
        packet1 = [0x22, 0x10, mask, 0x00] + color_bytes[:_COLOR_BYTES_PER_PACKET]
        packet2: list[int] | None = None
        if led_count > _LEDS_PER_PACKET:
            packet2 = [0x22, 0x11, mask, 0x00] + color_bytes[_COLOR_BYTES_PER_PACKET:]

        apply_packet = list(_APPLY_VARIANTS[apply_variant])
        apply_packet[_APPLY_MASK_OFFSET] = mask

        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("write_lighting_frame(%s): device not connected", channel)
                return False
            try:
                self._lighting_write(packet1)
                if packet2 is not None:
                    self._lighting_write(packet2)
                self._lighting_write(apply_packet)
                return True
            except Exception:
                logger.exception(
                    "write_lighting_frame(%s, %d LEDs) failed; marking disconnected",
                    channel,
                    led_count,
                )
                self._mark_disconnected()
                return False

    def _lighting_write(self, data: list[int]) -> None:
        """Send one HID OUT report through the driver's HID handle.

        Mirrors kraken3.py ``_write``: right-zero-pad ``data`` to the 64-byte
        report length and hand the list to the underlying hidapi handle
        (``self._dev.device.write``).  Caller must hold the lock; raises on I/O
        error (handled by the caller).
        """
        if len(data) > _LIGHTING_REPORT_LENGTH:
            # Defensive: never over-length a report (would corrupt framing).
            logger.warning(
                "lighting report of %d bytes truncated to %d",
                len(data),
                _LIGHTING_REPORT_LENGTH,
            )
            data = data[:_LIGHTING_REPORT_LENGTH]
        padding = [0x00] * (_LIGHTING_REPORT_LENGTH - len(data))
        self._dev.device.write(data + padding)

    def _lighting_read_until(
        self, prefix: tuple[int, int]
    ) -> list[int] | None:
        """Read 64-byte IN reports until one starts with ``prefix``.

        Mirrors kraken3.py ``_read``/``_read_until`` semantics: read up to
        :data:`_LIGHTING_MAX_READ_ATTEMPTS` reports and return the first whose
        first two bytes match ``prefix``; return ``None`` if none matched (the
        caller treats that as a non-fatal miss, NOT an I/O error).  Caller holds
        the lock; raises only on a genuine read I/O error.
        """
        for _ in range(_LIGHTING_MAX_READ_ATTEMPTS):
            msg = list(self._dev.device.read(_LIGHTING_REPORT_LENGTH))
            if len(msg) >= 2 and msg[0] == prefix[0] and msg[1] == prefix[1]:
                return msg
        return None

    # ----------------------------------------------------------------- helpers
    def _set_screen(self, mode: str, value: Any) -> bool:
        """Serialized ``dev.set_screen("lcd", mode, value)`` with error handling.

        ``value`` is passed as-is: int for brightness/orientation (the driver
        calls ``int(value)``), ``str`` path for static/gif, ``None`` for liquid.

        The driver raises two very different kinds of error from ``set_screen``:

        * *Recoverable* application/validation errors on a perfectly healthy,
          connected device -- an oversized GIF (``AssertionError`` "Max file size
          after resize is 24MB"), a missing/non-image path
          (``FileNotFoundError`` / ``PIL.UnidentifiedImageError``), an
          out-of-range brightness/orientation (``AssertionError``), etc.  These
          must **not** mark the device disconnected: doing so would trigger a
          spurious disconnect + reconnect storm (and a full re-apply) just because
          the user picked a 30 MB GIF or a deleted file.
        * Genuine *I/O* errors (USB/OS level) which mean the device really went
          away and the engine should attempt a reconnect.

        We therefore only call :meth:`_mark_disconnected` for the I/O kind.
        """
        # Image/GIF uploads can hit the swallowed bucket-transfer failure; watch
        # for it so we can clear buckets and retry. Brightness/orientation/liquid
        # don't use buckets, so they skip the watcher entirely.
        is_data_upload = mode in ("static", "gif")
        watcher = self._ensure_bucket_watcher() if is_data_upload else None

        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("set_screen(%s): device not connected", mode)
                return False
            try:
                if is_data_upload:
                    # Gate the driver's bucket switch so a contention-failed frame
                    # holds the last good image instead of flashing the firmware
                    # screen (see _install_lcd_switch_guard).
                    self._lcd_guard_active = True
                    self._lcd_setup_ok.clear()
                if watcher is not None:
                    watcher.reset()
                self._dev.set_screen("lcd", mode, value)
                # The driver swallows bucket-transfer failures (the switch guard
                # kept the bad frame off-screen, so the panel held its last good
                # image) and returns as if successful. Recover the content:
                #   * transient HID contention -- a plain re-upload usually lands;
                #   * bucket-memory exhaustion (~every 30 frames) -- clear buckets
                #     and retry (the guard suppresses the liquid switch, so even
                #     this is flash-free).
                if watcher is not None and watcher.failed:
                    if not self._retry_lcd_upload(mode, value, watcher):
                        return False
                logger.info("LCD set_screen mode=%s value=%r ok", mode, value)
                return True
            except _RECOVERABLE_SCREEN_ERRORS as exc:
                # Content/validation problem on a healthy device: surface it as a
                # plain failure WITHOUT disconnecting.
                logger.warning(
                    "set_screen(lcd, %s, %r) rejected (recoverable): %s",
                    mode,
                    value,
                    exc,
                )
                return False
            except Exception:
                logger.exception("set_screen(lcd, %s, %r) failed", mode, value)
                self._mark_disconnected()
                return False
            finally:
                self._lcd_guard_active = False

    def _retry_lcd_upload(self, mode: str, value: Any, watcher: _BucketFailureWatcher) -> bool:
        """Recover a failed LCD upload. Caller holds the lock and ``watcher.failed``.

        First retries the upload as-is (cheap, no visible glitch) to ride out
        transient HID contention; if those keep failing it's bucket exhaustion,
        so clear all buckets (brief liquid screen) and retry from a clean slate.
        Returns ``True`` once an upload succeeds, ``False`` if all attempts fail.
        """
        for attempt in range(1, _LCD_PLAIN_RETRIES + 1):
            logger.debug("LCD %s: upload failed; plain retry %d/%d", mode, attempt, _LCD_PLAIN_RETRIES)
            watcher.reset()
            self._dev.set_screen("lcd", mode, value)
            if not watcher.failed:
                return True
        for attempt in range(1, _LCD_CLEAR_RETRIES + 1):
            logger.info(
                "LCD %s: still failing after %d plain retries; clearing buckets (%d/%d)",
                mode,
                _LCD_PLAIN_RETRIES,
                attempt,
                _LCD_CLEAR_RETRIES,
            )
            self._dev._delete_all_buckets()  # noqa: SLF001
            watcher.reset()
            self._dev.set_screen("lcd", mode, value)
            if not watcher.failed:
                return True
        logger.warning("LCD %s: upload still failing after plain + clear retries", mode)
        return False

    def _clamp_duty(self, channel: str, duty: int) -> int:
        """Clamp ``duty`` to the limits of ``channel`` (defaults to fan limits)."""
        try:
            duty_int = int(duty)
        except (TypeError, ValueError):
            logger.warning("Non-integer duty %r for %s; using 0", duty, channel)
            duty_int = 0
        if channel == "pump":
            return _clamp(duty_int, self.PUMP_DUTY_MIN, self.PUMP_DUTY_MAX)
        return _clamp(duty_int, self.FAN_DUTY_MIN, self.FAN_DUTY_MAX)

    def _apply_init_status(self, init_status: Any) -> None:
        """Parse ``initialize()`` output (by label) into cached attributes."""
        parsed = _parse_tuples(init_status)

        fw = _get_str(parsed, _LBL_FIRMWARE)
        if fw is not None:
            self.firmware_version = fw

        brightness = _get_int(parsed, _LBL_LCD_BRIGHTNESS)
        if brightness is not None:
            self.lcd_brightness = _clamp(brightness, 0, 100)

        orientation = _get_int(parsed, _LBL_LCD_ORIENTATION)
        if orientation is not None:
            # initialize() reports orientation already in degrees (0/90/180/270).
            self.lcd_orientation = orientation if orientation in (0, 90, 180, 270) else 0

    def _mark_disconnected(self) -> None:
        """Tear down the current driver instance and reset connection state.

        Caller must hold the lock (or be ``__init__``).  Crucially this releases
        the *real* OS handles the ``KrakenZ3`` instance is holding before dropping
        the reference: the HID device (``dev.disconnect()``) **and** the separate
        pyusb bulk-out interface (``dev.bulk_device``).  Without releasing the bulk
        interface, a subsequent :func:`find_liquidctl_devices` would build a fresh
        ``KrakenZ3`` whose ``__init__`` tries to ``open()`` the still-claimed bulk
        interface and fails (``LIBUSB_ERROR_BUSY`` on Linux), so reconnection would
        fail permanently and leak a handle on every attempt.
        """
        dev = self._dev
        self._dev = None
        self._connected = False
        self._description = ""
        if dev is not None:
            self._safe_release(dev)

    @staticmethod
    def _safe_release(dev: Any) -> None:
        """Best-effort teardown of a driver instance's HID + bulk handles.

        Never raises.  Releases the bulk-out interface (pyusb ``release()`` on
        Linux / WinUSB ``close_winusb_device()`` on Windows) in addition to the
        HID device.
        """
        try:
            dev.disconnect()
        except Exception:
            logger.exception("Error while disconnecting device (ignored)")
        bulk = getattr(dev, "bulk_device", None)
        if bulk is None:
            return
        releaser = getattr(bulk, "release", None) or getattr(
            bulk, "close_winusb_device", None
        )
        if releaser is None:
            return
        try:
            releaser()
        except Exception:
            logger.exception("Error while releasing bulk interface (ignored)")

    # Backwards-compatible alias (older callers used ``_safe_disconnect``).
    _safe_disconnect = _safe_release


# --------------------------------------------------------------------------- #
# Module-level parsing helpers for liquidctl ``(label, value, unit)`` tuples.
# All match by case-insensitive label substring and never raise.
# --------------------------------------------------------------------------- #
def _parse_tuples(raw: Any) -> list[tuple[str, Any]]:
    """Coerce a liquidctl status/init list into ``[(lower_label, value), ...]``.

    Accepts the ``(label, value, unit)`` 3-tuples liquidctl emits (and tolerates
    2-tuples).  Returns an empty list for ``None`` or malformed input.
    """
    result: list[tuple[str, Any]] = []
    if not raw:
        return result
    try:
        items = list(raw)
    except TypeError:
        logger.warning("Cannot iterate status payload %r", type(raw))
        return result
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        label = item[0]
        value = item[1]
        if not isinstance(label, str):
            continue
        result.append((label.lower(), value))
    return result


def _find(parsed: list[tuple[str, Any]], fragment: str) -> Any | None:
    """Return the value whose label contains ``fragment`` (lower-cased), else None."""
    for label, value in parsed:
        if fragment in label:
            return value
    return None


def _get_float(parsed: list[tuple[str, Any]], fragment: str) -> float | None:
    value = _find(parsed, fragment)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Expected float for %r, got %r", fragment, value)
        return None


def _get_int(parsed: list[tuple[str, Any]], fragment: str) -> int | None:
    value = _find(parsed, fragment)
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        logger.warning("Expected int for %r, got %r", fragment, value)
        return None


def _get_str(parsed: list[tuple[str, Any]], fragment: str) -> str | None:
    value = _find(parsed, fragment)
    if value is None:
        return None
    return str(value)


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp ``value`` to the inclusive range ``[low, high]``."""
    return max(low, min(high, value))


def _parse_lighting_info(reply: list[int]) -> LightingInfo | None:
    """Parse a ``0x21 0x03`` reply into :class:`LightingInfo` (PROTOCOL.md §6).

    Layout: channel count at byte 14; accessory ids start at byte 15 with a
    stride of :data:`_HUE2_MAX_ACCESSORIES_IN_CHANNEL` (6) slots per channel
    (slot value 0 = empty).  We map our two known app channels (``ring`` =
    channel index 0, ``fans`` = channel index 1) onto their slots, regardless of
    the reported ``channel_count`` (which on this device is 2 -- see Quirk B; we
    deliberately model both channels rather than asserting equality).

    Each non-zero accessory id is looked up in :data:`_ACCESSORY_LED_COUNTS`
    (unknown ids -> :data:`_FALLBACK_ACCESSORY_LEDS` with a warning) and summed
    per channel.  Returns ``None`` (logged) for a reply too short to parse.
    """
    # Channel-name -> channel index (PROTOCOL.md §2).
    channel_indices = {"ring": 0, "fans": 1}

    # Need to reach the last slot of the highest channel index we read.
    max_index = max(channel_indices.values())
    min_len = (
        _LIGHTING_INFO_ACCESSORY_OFFSET
        + max_index * _HUE2_MAX_ACCESSORIES_IN_CHANNEL
        + _HUE2_MAX_ACCESSORIES_IN_CHANNEL
    )
    if not reply or len(reply) <= _LIGHTING_INFO_CHANNEL_COUNT_OFFSET:
        logger.warning(
            "Lighting-info reply too short (%d bytes); cannot parse",
            len(reply) if reply else 0,
        )
        return None

    channel_count = int(reply[_LIGHTING_INFO_CHANNEL_COUNT_OFFSET])

    accessories: dict[str, list[int]] = {}
    led_counts: dict[str, int] = {}
    for name, ch_index in channel_indices.items():
        ids: list[int] = []
        total = 0
        base = _LIGHTING_INFO_ACCESSORY_OFFSET + ch_index * _HUE2_MAX_ACCESSORIES_IN_CHANNEL
        for slot in range(_HUE2_MAX_ACCESSORIES_IN_CHANNEL):
            idx = base + slot
            if idx >= len(reply):
                break
            acc_id = int(reply[idx])
            if acc_id == 0:
                continue
            ids.append(acc_id)
            count = _ACCESSORY_LED_COUNTS.get(acc_id)
            if count is None:
                logger.warning(
                    "Unknown lighting accessory id 0x%02X on channel %r; assuming %d LEDs",
                    acc_id,
                    name,
                    _FALLBACK_ACCESSORY_LEDS,
                )
                count = _FALLBACK_ACCESSORY_LEDS
            total += count
        accessories[name] = ids
        # Fall back to the conservative default when a channel reports nothing
        # (e.g. a short reply that didn't include this channel's slots).
        led_counts[name] = total if total > 0 else _FALLBACK_LED_COUNTS.get(name, 0)

    if len(reply) < min_len:
        logger.warning(
            "Lighting-info reply length %d shorter than expected %d; "
            "parsed what was present, channels may be incomplete",
            len(reply),
            min_len,
        )

    return LightingInfo(
        channel_count=channel_count,
        accessories=accessories,
        led_counts=led_counts,
    )
