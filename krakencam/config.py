"""Persistent application configuration for Kraken CAM.

Defines the dataclasses describing the user's cooling/LCD/app preferences and
handles (de)serialization to a JSON file under ``~/.config/kraken-cam``.

All loading/saving is tolerant: a missing or corrupt config file falls back to
sensible defaults (logged as a warning) rather than raising, and unknown keys
in the on-disk JSON are ignored so that newer/older versions interoperate.
Tuples (curve points) round-trip through JSON as lists and are normalized back
to tuples in :meth:`AppConfig.from_dict`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Base directory for all persisted state.  The media cache (resized images,
#: rendered sensor frames the user has explicitly saved, etc.) lives under
#: ``DEFAULT_CONFIG_DIR / "media"``.
DEFAULT_CONFIG_DIR: Path = Path.home() / ".config" / "kraken-cam"

#: Default path of the JSON config file.
DEFAULT_CONFIG_PATH: Path = DEFAULT_CONFIG_DIR / "config.json"

#: Media cache directory (resized/static images, gifs, etc.).
MEDIA_DIR: Path = DEFAULT_CONFIG_DIR / "media"


#: Fallback lighting mode when an on-disk mode is unknown / unparsable.  The
#: authoritative set of modes lives in :mod:`krakencam.backend.lighting_fx`
#: (``MODES``); we validate against it tolerantly when importable and otherwise
#: only fall back on a non-string value (see :func:`_normalize_lighting_mode`).
_LIGHTING_DEFAULT_MODE: str = "fixed"

#: Default lighting colour (NZXT purple ``#7c3aed`` -> ``(124, 58, 237)``).
_LIGHTING_DEFAULT_COLOR: tuple[int, int, int] = (124, 58, 237)


def _default_lighting_colors() -> list[tuple[int, int, int]]:
    """Default lighting colour list (a single NZXT-purple swatch)."""
    return [_LIGHTING_DEFAULT_COLOR]


def _default_pump_points() -> list[tuple[float, int]]:
    """Default pump curve points (balanced preset, liquid-temp keyed)."""
    return [(20.0, 50), (33.0, 60), (40.0, 75), (46.0, 90), (50.0, 100)]


def _default_fan_points() -> list[tuple[float, int]]:
    """Default fan curve points (balanced preset, liquid-temp keyed)."""
    return [(20.0, 30), (33.0, 40), (40.0, 60), (45.0, 80), (50.0, 100)]


@dataclass
class ChannelConfig:
    """Configuration for a single speed channel (``pump`` or ``fan``).

    ``points`` is a list of ``(temp_c, duty_pct)`` tuples sorted by temperature
    and is only meaningful in ``curve`` mode.  ``source`` selects which sensor
    drives the x-axis of the curve.
    """

    mode: str = "curve"  # "curve" | "fixed"
    source: str = "liquid"  # "liquid" | "cpu" | "gpu"  (x-axis input for curve mode)
    fixed_duty: int = 50
    points: list[tuple[float, int]] = field(default_factory=_default_pump_points)
    profile: str = "balanced"  # "silent" | "balanced" | "performance" | "fixed" | "custom"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (points become lists)."""
        return {
            "mode": self.mode,
            "source": self.source,
            "fixed_duty": int(self.fixed_duty),
            "points": [[float(t), int(d)] for (t, d) in self.points],
            "profile": self.profile,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, defaults: "ChannelConfig | None" = None) -> "ChannelConfig":
        """Build a :class:`ChannelConfig` from a (possibly partial) mapping.

        Unknown keys are ignored; missing keys fall back to ``defaults`` (or the
        dataclass defaults).  Curve points stored as lists are normalized to
        tuples of ``(float, int)``.
        """
        base = defaults if defaults is not None else cls()
        if not isinstance(d, dict):
            logger.warning("ChannelConfig.from_dict received non-dict %r; using defaults", type(d))
            return base

        points = base.points
        raw_points = d.get("points")
        if raw_points is not None:
            points = _normalize_points(raw_points, fallback=base.points)

        return cls(
            mode=_as_str(d.get("mode"), base.mode),
            source=_as_str(d.get("source"), base.source),
            fixed_duty=_as_int(d.get("fixed_duty"), base.fixed_duty),
            points=points,
            profile=_as_str(d.get("profile"), base.profile),
        )


@dataclass
class LcdConfig:
    """Configuration for the round 640x640 LCD."""

    mode: str = "liquid"  # "liquid" | "sensors" | "static" | "gif" | "off"
    brightness: int = 50  # 0-100
    orientation: int = 0  # 0 | 90 | 180 | 270
    image_path: str = ""  # last chosen static image (absolute path)
    gif_path: str = ""  # last chosen gif (absolute path)
    sensor_style: str = "liquid_ring"  # see lcd_render.STYLES
    sensor_interval: float = 2.0  # seconds between sensor-screen pushes

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "mode": self.mode,
            "brightness": int(self.brightness),
            "orientation": int(self.orientation),
            "image_path": self.image_path,
            "gif_path": self.gif_path,
            "sensor_style": self.sensor_style,
            "sensor_interval": float(self.sensor_interval),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, defaults: "LcdConfig | None" = None) -> "LcdConfig":
        """Build an :class:`LcdConfig` from a (possibly partial) mapping."""
        base = defaults if defaults is not None else cls()
        if not isinstance(d, dict):
            logger.warning("LcdConfig.from_dict received non-dict %r; using defaults", type(d))
            return base

        brightness = _clamp_int(_as_int(d.get("brightness"), base.brightness), 0, 100)
        orientation = _as_int(d.get("orientation"), base.orientation)
        if orientation not in (0, 90, 180, 270):
            logger.warning("Invalid LCD orientation %r; using %d", orientation, base.orientation)
            orientation = base.orientation

        return cls(
            mode=_as_str(d.get("mode"), base.mode),
            brightness=brightness,
            orientation=orientation,
            image_path=_as_str(d.get("image_path"), base.image_path),
            gif_path=_as_str(d.get("gif_path"), base.gif_path),
            sensor_style=_as_str(d.get("sensor_style"), base.sensor_style),
            sensor_interval=_as_float(d.get("sensor_interval"), base.sensor_interval),
        )


@dataclass
class LightingChannelConfig:
    """Lighting configuration for a single RGB channel (``ring`` or ``fans``).

    ``mode`` is a key into :data:`krakencam.backend.lighting_fx.MODES`.  ``colors``
    is a list of ``(r, g, b)`` triplets (each component 0-255); the count the
    device actually uses is clamped to the active mode's min/max by the effect
    engine, not here.  ``brightness`` (0-100) is applied **host-side** — the device
    has no brightness command.  ``speed`` selects the animation step rate.
    """

    mode: str = _LIGHTING_DEFAULT_MODE  # key into lighting_fx.MODES
    colors: list[tuple[int, int, int]] = field(default_factory=_default_lighting_colors)
    brightness: int = 100  # 0-100, applied host-side
    speed: str = "normal"  # "slow" | "normal" | "fast"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (colours become lists of 3 ints)."""
        return {
            "mode": self.mode,
            "colors": [[int(r), int(g), int(b)] for (r, g, b) in self.colors],
            "brightness": int(self.brightness),
            "speed": self.speed,
        }

    @classmethod
    def from_dict(
        cls, d: dict[str, Any], *, defaults: "LightingChannelConfig | None" = None
    ) -> "LightingChannelConfig":
        """Build a :class:`LightingChannelConfig` from a (possibly partial) mapping.

        Unknown keys are ignored; missing keys fall back to ``defaults`` (or the
        dataclass defaults).  Colours stored as lists are normalized to tuples of
        three ints clamped to 0-255, ``brightness`` is clamped to 0-100, and an
        unknown ``mode`` falls back to ``"fixed"``.
        """
        base = defaults if defaults is not None else cls()
        if not isinstance(d, dict):
            logger.warning(
                "LightingChannelConfig.from_dict received non-dict %r; using defaults",
                type(d),
            )
            return base

        colors = base.colors
        raw_colors = d.get("colors")
        if raw_colors is not None:
            colors = _normalize_colors(raw_colors, fallback=base.colors)

        return cls(
            mode=_normalize_lighting_mode(d.get("mode"), base.mode),
            colors=colors,
            brightness=_clamp_int(_as_int(d.get("brightness"), base.brightness), 0, 100),
            speed=_as_str(d.get("speed"), base.speed),
        )


@dataclass
class LightingConfig:
    """Top-level RGB lighting configuration (pump ring + RGB Core fan channel).

    ``enabled`` defaults to ``False`` so the app never touches the LEDs until the
    user opts in.  When ``sync`` is ``True`` the ``ring`` configuration drives both
    channels and ``fans`` is ignored by the engine.
    """

    enabled: bool = False  # False = app never touches LEDs
    sync: bool = True  # True = ring config drives both channels
    ring: LightingChannelConfig = field(default_factory=LightingChannelConfig)
    fans: LightingChannelConfig = field(default_factory=LightingChannelConfig)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "enabled": bool(self.enabled),
            "sync": bool(self.sync),
            "ring": self.ring.to_dict(),
            "fans": self.fans.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, d: dict[str, Any], *, defaults: "LightingConfig | None" = None
    ) -> "LightingConfig":
        """Build a :class:`LightingConfig` from a (possibly partial) mapping."""
        base = defaults if defaults is not None else cls()
        if not isinstance(d, dict):
            logger.warning(
                "LightingConfig.from_dict received non-dict %r; using defaults", type(d)
            )
            return base

        ring_raw = d.get("ring")
        fans_raw = d.get("fans")
        ring = (
            LightingChannelConfig.from_dict(ring_raw, defaults=base.ring)
            if isinstance(ring_raw, dict)
            else base.ring
        )
        fans = (
            LightingChannelConfig.from_dict(fans_raw, defaults=base.fans)
            if isinstance(fans_raw, dict)
            else base.fans
        )

        return cls(
            enabled=_as_bool(d.get("enabled"), base.enabled),
            sync=_as_bool(d.get("sync"), base.sync),
            ring=ring,
            fans=fans,
        )


@dataclass
class AppConfig:
    """Top-level application configuration, persisted as a single JSON file."""

    poll_interval: float = 1.0
    history_seconds: int = 600
    start_minimized: bool = False
    close_to_tray: bool = True
    #: Keep the control engine running (cooling/lighting/LCD stay active) when the
    #: window is closed on a desktop with no system tray, instead of quitting.
    #: The window is hidden and re-launching Kraken CAM reopens it.
    run_in_background: bool = True
    apply_on_start: bool = True
    pump: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(
            mode="curve",
            source="liquid",
            fixed_duty=50,
            points=_default_pump_points(),
            profile="balanced",
        )
    )
    fan: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(
            mode="curve",
            source="liquid",
            fixed_duty=50,
            points=_default_fan_points(),
            profile="balanced",
        )
    )
    lcd: LcdConfig = field(default_factory=LcdConfig)
    lighting: LightingConfig = field(default_factory=LightingConfig)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        """Load config from ``path`` (default :data:`DEFAULT_CONFIG_PATH`).

        A missing or corrupt file results in default configuration (and a
        logged warning); this method never raises.
        """
        cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            logger.info("No config file at %s; using defaults", cfg_path)
            return cls()
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to read/parse config %s (%s); using defaults", cfg_path, exc)
            return cls()
        if not isinstance(data, dict):
            logger.warning("Config %s is not a JSON object; using defaults", cfg_path)
            return cls()
        try:
            return cls.from_dict(data)
        except Exception:  # pragma: no cover - defensive; from_dict is tolerant
            logger.exception("Unexpected error parsing config %s; using defaults", cfg_path)
            return cls()

    # ------------------------------------------------------------------ save
    def save(self, path: Path | None = None) -> None:
        """Atomically persist this config to ``path``.

        Creates the parent directory if needed and writes via a temp file +
        ``os.replace`` so a crash mid-write cannot corrupt the existing config.
        """
        cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        cfg_dir = cfg_path.parent
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Could not create config directory %s", cfg_dir)
            return

        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=cfg_path.name + ".", suffix=".tmp", dir=str(cfg_dir)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, cfg_path)
            logger.debug("Saved config to %s", cfg_path)
        except OSError:
            logger.exception("Failed to write config to %s", cfg_path)
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                logger.debug("Could not remove temp config file %s", tmp_name)

    # ------------------------------------------------------------ (de)serialize
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of the whole configuration."""
        return {
            "poll_interval": float(self.poll_interval),
            "history_seconds": int(self.history_seconds),
            "start_minimized": bool(self.start_minimized),
            "close_to_tray": bool(self.close_to_tray),
            "run_in_background": bool(self.run_in_background),
            "apply_on_start": bool(self.apply_on_start),
            "pump": self.pump.to_dict(),
            "fan": self.fan.to_dict(),
            "lcd": self.lcd.to_dict(),
            "lighting": self.lighting.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppConfig":
        """Build an :class:`AppConfig` from a (possibly partial) mapping.

        Tolerant of unknown keys (ignored) and missing keys (defaults). Curve
        points round-tripped as lists are normalized back to tuples.
        """
        defaults = cls()
        if not isinstance(d, dict):
            logger.warning("AppConfig.from_dict received non-dict %r; using defaults", type(d))
            return defaults

        pump_raw = d.get("pump")
        fan_raw = d.get("fan")
        lcd_raw = d.get("lcd")
        lighting_raw = d.get("lighting")

        pump = (
            ChannelConfig.from_dict(pump_raw, defaults=defaults.pump)
            if isinstance(pump_raw, dict)
            else defaults.pump
        )
        fan = (
            ChannelConfig.from_dict(fan_raw, defaults=defaults.fan)
            if isinstance(fan_raw, dict)
            else defaults.fan
        )
        lcd = (
            LcdConfig.from_dict(lcd_raw, defaults=defaults.lcd)
            if isinstance(lcd_raw, dict)
            else defaults.lcd
        )
        lighting = (
            LightingConfig.from_dict(lighting_raw, defaults=defaults.lighting)
            if isinstance(lighting_raw, dict)
            else defaults.lighting
        )

        return cls(
            poll_interval=_as_float(d.get("poll_interval"), defaults.poll_interval),
            history_seconds=_as_int(d.get("history_seconds"), defaults.history_seconds),
            start_minimized=_as_bool(d.get("start_minimized"), defaults.start_minimized),
            close_to_tray=_as_bool(d.get("close_to_tray"), defaults.close_to_tray),
            run_in_background=_as_bool(d.get("run_in_background"), defaults.run_in_background),
            apply_on_start=_as_bool(d.get("apply_on_start"), defaults.apply_on_start),
            pump=pump,
            fan=fan,
            lcd=lcd,
            lighting=lighting,
        )


# --------------------------------------------------------------------------- #
# Internal coercion helpers — all tolerant, never raise.
# --------------------------------------------------------------------------- #
def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):  # bool is a subclass of int; treat explicitly
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Expected int, got %r; using default %r", value, default)
        return default


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Expected float, got %r; using default %r", value, default)
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _normalize_lighting_mode(value: Any, default: str) -> str:
    """Coerce a lighting ``mode`` value, falling back to ``"fixed"`` if unknown.

    A non-string value (or ``None``) yields ``default``.  A string value is
    validated against :data:`krakencam.backend.lighting_fx.MODES` when that module
    is importable; an unrecognized key falls back to :data:`_LIGHTING_DEFAULT_MODE`
    ("fixed").  If ``lighting_fx`` cannot be imported (e.g. during isolated config
    round-trips) the string is accepted as-is so we do not silently discard a valid
    mode merely because the effect engine is unavailable.
    """
    if not isinstance(value, str):
        if value is None:
            return default
        logger.warning("Lighting mode is not a string (%r); using %r", value, default)
        return default
    # Local import to avoid import-order coupling between config and the effect
    # engine (mirrors the curves import in _normalize_points).
    try:
        from krakencam.backend import lighting_fx
    except Exception:  # pragma: no cover - lighting_fx optional at config-parse time
        return value
    if value in lighting_fx.MODES:
        return value
    logger.warning("Unknown lighting mode %r; using %r", value, _LIGHTING_DEFAULT_MODE)
    return _LIGHTING_DEFAULT_MODE


def _normalize_colors(
    raw: Any, *, fallback: list[tuple[int, int, int]]
) -> list[tuple[int, int, int]]:
    """Normalize a lighting colour list from JSON into ``(r, g, b)`` int tuples.

    Each colour is coerced to three ints clamped to 0-255.  Malformed entries are
    skipped.  An *explicitly empty* list round-trips to ``[]`` (swatch-less modes
    like ``off``/``spectrum`` persist no colours and must reload identically); the
    ``fallback`` is only used when the input is not a list at all, or when a
    *non-empty* input contained nothing usable.  The list length is *not* clamped
    to a mode's min/max here — that is the effect engine's responsibility (it
    depends on the active mode).
    """
    if not isinstance(raw, (list, tuple)):
        logger.warning("Lighting colours are not a list (%r); using fallback", type(raw))
        return [tuple(c) for c in fallback]

    # An explicitly empty list is a legitimate value (off/spectrum keep no
    # colours); preserve it rather than substituting the fallback.
    if len(raw) == 0:
        return []

    colors: list[tuple[int, int, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            logger.warning("Skipping malformed colour %r", item)
            continue
        try:
            r = _clamp_int(int(round(float(item[0]))), 0, 255)
            g = _clamp_int(int(round(float(item[1]))), 0, 255)
            b = _clamp_int(int(round(float(item[2]))), 0, 255)
        except (TypeError, ValueError):
            logger.warning("Skipping non-numeric colour %r", item)
            continue
        colors.append((r, g, b))

    if not colors:
        # Non-empty input that contained nothing usable: fall back.
        logger.warning("No valid lighting colours parsed; using fallback")
        return [tuple(c) for c in fallback]
    return colors


def _normalize_points(
    raw: Any, *, fallback: list[tuple[float, int]]
) -> list[tuple[float, int]]:
    """Normalize curve points from JSON (lists) into tuples of ``(float, int)``.

    Invalid entries are skipped.  If nothing usable remains, ``fallback`` is
    returned (copied).  The result is sorted by temperature.
    """
    if not isinstance(raw, (list, tuple)):
        logger.warning("Curve points are not a list (%r); using fallback", type(raw))
        return [(float(t), int(d)) for (t, d) in fallback]

    points: list[tuple[float, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            logger.warning("Skipping malformed curve point %r", item)
            continue
        try:
            temp = float(item[0])
            # Round rather than truncate so 60.9 -> 61 (matches validate_points).
            duty = int(round(float(item[1])))
        except (TypeError, ValueError):
            logger.warning("Skipping non-numeric curve point %r", item)
            continue
        points.append((temp, duty))

    if not points:
        logger.warning("No valid curve points parsed; using fallback")
        return [(float(t), int(d)) for (t, d) in fallback]

    # Run the parsed points through the canonical validator so a hand-corrupted
    # config is already clamped (temp 0-99, duty 0-100), deduped, sorted and at
    # least two points long before it reaches the GUI / engine -- matching the
    # invariant the rest of the app assumes (curves.validate_points).  Local
    # import to avoid any import-order coupling between config and curves.
    from krakencam.backend import curves

    return curves.validate_points(points)
