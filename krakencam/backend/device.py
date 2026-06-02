"""Thread-safe wrapper around the liquidctl Kraken Z3 driver.

This is the single choke-point for *all* device I/O in Kraken CAM.  Every public
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
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

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
            self._apply_init_status(init_status)
            logger.info(
                "Connected to %s (fw=%s, brightness=%d%%, orientation=%d°)",
                self._description or "Kraken device",
                self.firmware_version or "?",
                self.lcd_brightness,
                self.lcd_orientation,
            )
            return True

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

    def set_lcd_liquid_mode(self) -> bool:
        """Switch the LCD to the firmware liquid-temperature screen."""
        return self._set_screen("liquid", None)

    def set_lcd_static(self, image_path: str) -> bool:
        """Upload a static image to the LCD (blocking; ~0.1-1 s)."""
        return self._set_screen("static", str(image_path))

    def set_lcd_gif(self, gif_path: str) -> bool:
        """Upload an animated GIF to the LCD (blocking)."""
        return self._set_screen("gif", str(gif_path))

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
        with self._lock:
            if self._dev is None or not self._connected:
                logger.warning("set_screen(%s): device not connected", mode)
                return False
            try:
                self._dev.set_screen("lcd", mode, value)
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
