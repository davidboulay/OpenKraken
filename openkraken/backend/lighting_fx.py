"""Host-side LED effect engine for OpenKraken.

This module is pure Python (no Qt, no I/O) and computes per-LED RGB frames for
the NZXT Kraken 2024 Elite RGB.  The device's *hardware* effect/animation modes
are rejected by its firmware (see ``PROTOCOL.md`` §7), so every effect -- even a
plain solid colour -- is realised as a full per-LED "Direct" frame streamed from
the host (``PROTOCOL.md`` §4/§5).

Because the device only reliably accepts Direct frames at roughly **1 frame per
second** (``PROTOCOL.md`` §5, OpenRGB #4828), the animated modes here are
designed to look acceptable when *sampled at 1 Hz*: slow sinusoidal breathing,
gentle cross-fades and a slow hue rotation -- never fast flashes or hard steps
that would alias into stutter at one update per second.

Public surface
--------------
* :class:`ModeSpec` -- static description of an effect (key, label, colour
  count bounds, whether it animates).
* :data:`MODES` -- the available effects keyed by ``ModeSpec.key``.
* :data:`SPEED_PERIODS` -- seconds-per-cycle for each named animation speed.
* :func:`frame` -- the one entry point: given a mode, the user's colour list, a
  host-side brightness (0-100), the channel's LED count, and elapsed seconds
  ``t``, return exactly ``led_count`` RGB triplets with brightness already
  applied.  Pure and deterministic; ``frame(...)`` repeats with the mode's
  period in ``t`` for animated modes.

Conventions
-----------
* Colours are ``(r, g, b)`` tuples, each component an ``int`` in ``0..255``.
  GRB-on-the-wire conversion and the actual HID framing live entirely in
  ``device.py`` -- nothing here knows about the wire format.
* Brightness is applied **host-side** (the device has no brightness command):
  every returned component is scaled by ``brightness / 100`` and clamped.
* ``t`` is "seconds since the mode was (re)applied"; the engine supplies a
  monotonic elapsed time.  ``t`` may be any non-negative float.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

__all__ = [
    "ModeSpec",
    "MODES",
    "SPEED_PERIODS",
    "DEFAULT_SPEED",
    "frame",
    "clamp_colors",
    "scale_color",
]

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModeSpec:
    """Static description of a lighting effect.

    ``min_colors`` / ``max_colors`` bound how many user colours the mode
    consumes; the GUI clamps the swatch count to this range and :func:`frame`
    tolerates lists outside it (padding / trimming / falling back as needed).
    ``animated`` is ``True`` when the frame depends on ``t`` (the engine must
    then stream frames each tick rather than write once).
    """

    key: str
    label: str
    min_colors: int
    max_colors: int
    animated: bool


#: Available effects, keyed by :attr:`ModeSpec.key`.  Mirrors the contract in
#: ``INTERFACES-LIGHTING.md``.  Every animated mode is tuned to look good when
#: sampled at ~1 Hz (``PROTOCOL.md`` §5).
MODES: dict[str, ModeSpec] = {
    "off": ModeSpec("off", "Off", 0, 0, False),
    "fixed": ModeSpec("fixed", "Fixed", 1, 1, False),
    # Cycles through the colour list while a slow sine curve dims the whole ring.
    "breathing": ModeSpec("breathing", "Breathing", 1, 4, True),
    # Smooth cross-fade travelling through the colour list.
    "cycle": ModeSpec("cycle", "Color cycle", 2, 8, True),
    # Hue rotation distributed around the ring; ignores the colour list.
    "spectrum": ModeSpec("spectrum", "Spectrum wave", 0, 0, True),
}

#: Seconds for one full animation cycle, per named speed.  Long periods keep
#: 1 Hz sampling smooth (a 6 s cycle = ~6 sampled steps per loop).
SPEED_PERIODS: dict[str, float] = {"slow": 12.0, "normal": 6.0, "fast": 3.0}

#: Fallback when a speed name is unknown.
DEFAULT_SPEED: str = "normal"

#: Default colour used when a mode needs at least one colour but none was given
#: (NZXT purple, matching the app accent ``#7c3aed``).
_FALLBACK_COLOR: tuple[int, int, int] = (124, 58, 237)

_BLACK: tuple[int, int, int] = (0, 0, 0)


# --------------------------------------------------------------------------- #
# Small numeric helpers (all clamp; none ever raise on sane numeric input).
# --------------------------------------------------------------------------- #
def _clamp_int(value: float, low: int, high: int) -> int:
    """Round ``value`` to an int and clamp to the inclusive ``[low, high]``."""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        v = low
    return max(low, min(high, v))


def _clamp_channel(value: float) -> int:
    """Clamp a single colour component to ``0..255`` (as an int)."""
    return _clamp_int(value, 0, 255)


def _clamp_color(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Clamp every component of one ``(r, g, b)`` triplet to ``0..255``."""
    r, g, b = color
    return (_clamp_channel(r), _clamp_channel(g), _clamp_channel(b))


def scale_color(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Scale ``color`` by ``factor`` (>= 0) and clamp each component to 0-255.

    Used for host-side brightness and for dimming/breathing curves.  ``factor``
    is clamped to ``>= 0``; values above 1.0 are allowed but components still
    saturate at 255.
    """
    f = max(0.0, float(factor))
    r, g, b = color
    return (_clamp_channel(r * f), _clamp_channel(g * f), _clamp_channel(b * f))


def clamp_colors(
    colors: list[tuple[int, int, int]],
) -> list[tuple[int, int, int]]:
    """Return ``colors`` with every component clamped to ``0..255``.

    Tolerates ``None``/empty input (returns ``[]``) and malformed triplets are
    skipped with a warning, so a bad config entry can never crash a frame.
    """
    if not colors:
        return []
    out: list[tuple[int, int, int]] = []
    for c in colors:
        if not isinstance(c, (list, tuple)) or len(c) < 3:
            _LOGGER.warning("Skipping malformed colour %r", c)
            continue
        out.append(_clamp_color((c[0], c[1], c[2])))
    return out


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (h in turns 0..1, s/v in 0..1) to an ``(r, g, b)`` triplet.

    ``h`` is taken modulo 1.0, so any phase value is valid.  Returns ints in
    ``0..255``.
    """
    h = h % 1.0
    s = max(0.0, min(1.0, s))
    v = max(0.0, min(1.0, v))
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i %= 6
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return (_clamp_channel(r * 255.0), _clamp_channel(g * 255.0), _clamp_channel(b * 255.0))


def _period_for(speed: str) -> float:
    """Return the cycle period (seconds) for ``speed``, defaulting safely."""
    return SPEED_PERIODS.get(speed, SPEED_PERIODS[DEFAULT_SPEED])


def _resolve_colors(
    colors: list[tuple[int, int, int]], minimum: int
) -> list[tuple[int, int, int]]:
    """Clamp ``colors`` and guarantee at least ``minimum`` entries.

    When fewer than ``minimum`` colours are supplied the list is padded by
    repeating its own entries (or the fallback purple if empty), so animated
    modes always have something to work with.
    """
    resolved = clamp_colors(colors)
    if not resolved:
        resolved = [_FALLBACK_COLOR]
    if len(resolved) < minimum:
        # Pad by cycling through the supplied colours.
        original = list(resolved)
        while len(resolved) < minimum:
            resolved.append(original[len(resolved) % len(original)])
    return resolved


# --------------------------------------------------------------------------- #
# Per-mode frame builders.  Each returns a list of `count` UNSCALED triplets;
# `frame()` applies host-side brightness afterwards.
# --------------------------------------------------------------------------- #
def _frame_fixed(
    colors: list[tuple[int, int, int]], count: int
) -> list[tuple[int, int, int]]:
    """Solid colour: every LED takes the first supplied colour."""
    base = colors[0] if colors else _FALLBACK_COLOR
    base = _clamp_color(base)
    return [base] * count


def _frame_breathing(
    colors: list[tuple[int, int, int]], count: int, t: float, period: float
) -> list[tuple[int, int, int]]:
    """Whole-ring breathing: a slow sine dims a colour in/out.

    With more than one colour, each *breath* uses the next colour in the list,
    so the ring slowly walks the palette one inhale at a time.  The dim curve is
    a raised cosine in ``[BREATHE_FLOOR, 1.0]`` so the LEDs never fully blink off
    (full-off frames look like dropped frames at 1 Hz).
    """
    breathe_floor = 0.12
    # One full breath per `period`; phase 0..1 over the breath.
    breaths = t / period if period > 0 else 0.0
    phase = breaths - math.floor(breaths)
    # Raised cosine: bright at phase 0, dim at phase 0.5, bright again at 1.0.
    dim = breathe_floor + (1.0 - breathe_floor) * (0.5 + 0.5 * math.cos(2.0 * math.pi * phase))
    # Pick the colour for this breath (slowly walks the palette).
    idx = int(math.floor(breaths)) % len(colors)
    base = _clamp_color(colors[idx])
    lit = scale_color(base, dim)
    return [lit] * count


def _frame_cycle(
    colors: list[tuple[int, int, int]], count: int, t: float, period: float
) -> list[tuple[int, int, int]]:
    """Whole-ring cross-fade travelling smoothly through the colour list.

    The full list is traversed once per ``period * len(colors)`` (each adjacent
    pair gets ``period`` seconds to blend), giving a slow, even drift that reads
    well at 1 Hz.
    """
    n = len(colors)
    if n == 1:
        base = _clamp_color(colors[0])
        return [base] * count
    # Position along the ring of colours, in [0, n).
    pos = (t / period) % n if period > 0 else 0.0
    lo = int(math.floor(pos)) % n
    hi = (lo + 1) % n
    frac = pos - math.floor(pos)
    c0 = _clamp_color(colors[lo])
    c1 = _clamp_color(colors[hi])
    blended = (
        _clamp_channel(c0[0] + (c1[0] - c0[0]) * frac),
        _clamp_channel(c0[1] + (c1[1] - c0[1]) * frac),
        _clamp_channel(c0[2] + (c1[2] - c0[2]) * frac),
    )
    return [blended] * count


def _frame_spectrum(count: int, t: float, period: float) -> list[tuple[int, int, int]]:
    """Hue rotation distributed around the ring (a slow rainbow wave).

    The whole hue wheel is spread across the ring and rotates once per
    ``period``.  At 1 Hz this looks like the rainbow gently turning; per-LED
    hue offset means even a single sampled frame is colourful rather than flat.
    """
    if count <= 0:
        return []
    rotation = (t / period) if period > 0 else 0.0
    out: list[tuple[int, int, int]] = []
    for i in range(count):
        hue = (i / count) + rotation
        out.append(_hsv_to_rgb(hue, 1.0, 1.0))
    return out


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #
def frame(
    mode: str,
    colors: list[tuple[int, int, int]],
    brightness: int,
    led_count: int,
    t: float,
    speed: str = DEFAULT_SPEED,
) -> list[tuple[int, int, int]]:
    """Compute one LED frame.

    Parameters
    ----------
    mode:
        Key into :data:`MODES`.  Unknown modes are treated as ``"off"`` (logged).
    colors:
        User colour list, ``(r, g, b)`` triplets 0-255.  Clamped internally;
        padded/trimmed per the mode as needed.
    brightness:
        Host-side brightness 0-100, applied (clamped) to every component after
        the effect is computed.
    led_count:
        Number of LEDs on the target channel (e.g. 24 for the ring).  Values
        ``<= 0`` yield an empty frame.
    t:
        Seconds since the mode was applied (monotonic elapsed time).  Ignored by
        non-animated modes.
    speed:
        Animation speed name (key into :data:`SPEED_PERIODS`); ignored by
        non-animated modes.  Unknown names fall back to ``"normal"``.

    Returns
    -------
    list[tuple[int, int, int]]
        Exactly ``max(led_count, 0)`` brightness-scaled RGB triplets.
    """
    count = led_count if isinstance(led_count, int) and led_count > 0 else 0
    if count <= 0:
        return []

    spec = MODES.get(mode)
    if spec is None:
        _LOGGER.warning("Unknown lighting mode %r; treating as 'off'", mode)
        spec = MODES["off"]

    period = _period_for(speed)

    if spec.key == "off":
        raw = [_BLACK] * count
    elif spec.key == "fixed":
        raw = _frame_fixed(clamp_colors(colors), count)
    elif spec.key == "breathing":
        raw = _frame_breathing(_resolve_colors(colors, 1), count, max(0.0, float(t)), period)
    elif spec.key == "cycle":
        raw = _frame_cycle(_resolve_colors(colors, 2), count, max(0.0, float(t)), period)
    elif spec.key == "spectrum":
        raw = _frame_spectrum(count, max(0.0, float(t)), period)
    else:  # pragma: no cover - defensive; MODES is closed
        raw = [_BLACK] * count

    # Host-side brightness, applied uniformly and clamped (PROTOCOL.md §4).
    b = _clamp_int(brightness, 0, 100)
    if b == 100:
        return [_clamp_color(c) for c in raw]
    factor = b / 100.0
    return [scale_color(c, factor) for c in raw]
