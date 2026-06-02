"""Fan/pump duty curve maths for OpenKraken.

This module is pure Python (no Qt, no I/O) and provides:

* :data:`PROFILES` -- the built-in liquid-temperature presets used by the GUI
  and the control engine.
* :func:`interpolate` -- clamped piecewise-linear interpolation of a duty curve.
* :func:`validate_points` -- normalise a user-supplied point list into a clean,
  sorted, deduplicated, clamped curve with at least two points.
* :func:`software_failsafe` -- build a firmware-native liquid-temp profile that
  holds a software-computed duty but always ramps to 100 % before the loop can
  cook, so a crashed/killed app can never leave the cooler stuck low.
* :class:`DutySmoother` -- hysteresis that jumps duty up immediately for safety
  but ramps it down slowly, and suppresses tiny changes to avoid spamming the
  device with HID writes.

All curves are expressed as ``list[tuple[temp_c, duty_pct]]``; temperatures are
floats and duties are integer percentages.  The cooler firmware stores a 40-point
curve for liquid temperatures 20..59 C and runs it autonomously, so liquid-source
curves are written once and persist after the app exits.  CPU/GPU-source curves
are computed in software each tick and pushed as :func:`software_failsafe`
profiles.
"""

from __future__ import annotations

import logging

__all__ = [
    "PROFILES",
    "DUTY_MIN",
    "DUTY_MAX",
    "TEMP_MIN",
    "TEMP_MAX",
    "CRITICAL_TEMP",
    "interpolate",
    "validate_points",
    "software_failsafe",
    "DutySmoother",
]

_LOGGER = logging.getLogger(__name__)

# Built-in liquid-temperature keyed presets.  Pump duty is never below 20 %
# (the firmware clamp); fan may go down to 0 %.  Each preset reaches 100 % well
# before the firmware CRITICAL_TEMPERATURE (59 C) so the loop is always
# protected even if the app stops touching the device.
# Calibrated against the real loop: this machine idles around 40 C liquid at
# the stock ~40 % duty, and liquid only approaches 50 C under sustained load.
PROFILES: dict[str, dict[str, list[tuple[float, int]]]] = {
    "silent": {
        "pump": [(20, 40), (34, 50), (40, 60), (46, 85), (50, 100)],
        "fan": [(20, 20), (34, 30), (40, 45), (46, 70), (50, 100)],
    },
    "balanced": {
        "pump": [(20, 50), (33, 60), (40, 75), (46, 90), (50, 100)],
        "fan": [(20, 30), (33, 40), (40, 60), (45, 80), (50, 100)],
    },
    "performance": {
        "pump": [(20, 70), (30, 80), (38, 95), (42, 100)],
        "fan": [(20, 50), (30, 65), (38, 85), (44, 100)],
    },
}

# Duty/temperature bounds shared across the module.
DUTY_MIN: int = 0
DUTY_MAX: int = 100
TEMP_MIN: int = 0
TEMP_MAX: int = 99
CRITICAL_TEMP: int = 59


def interpolate(points: list[tuple[float, int]], x: float) -> float:
    """Clamped piecewise-linear interpolation of a duty curve at ``x``.

    ``points`` is a list of ``(temp_c, duty_pct)`` pairs.  The list does not have
    to be sorted; it is sorted internally by temperature.  For ``x`` below the
    first point or above the last point the curve is clamped to the respective
    end duty (i.e. the curve is flat outside its defined range).

    An empty list yields a neutral ``50.0`` so callers always get a usable duty.

    Returns a float; callers that need an integer duty should ``round`` it.
    """
    if not points:
        return 50.0

    ordered = sorted(points, key=lambda p: p[0])

    # Clamp below the first / above the last defined temperature.
    if x <= ordered[0][0]:
        return float(ordered[0][1])
    if x >= ordered[-1][0]:
        return float(ordered[-1][1])

    for (t0, d0), (t1, d1) in zip(ordered, ordered[1:]):
        if t0 <= x <= t1:
            span = t1 - t0
            if span <= 0:
                # Duplicate temperatures: use the later point's duty.
                return float(d1)
            frac = (x - t0) / span
            return float(d0) + frac * (float(d1) - float(d0))

    # Unreachable given the clamps above, but keep a safe fallback.
    return float(ordered[-1][1])


def validate_points(points: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """Normalise a user-supplied curve into a clean, applyable point list.

    Steps:

    * coerce each pair to ``(float temp, int duty)``, skipping anything that
      cannot be coerced;
    * clamp temperatures to ``0..99`` and duties to ``0..100``;
    * sort by temperature and drop duplicate temperatures (last value wins);
    * guarantee at least two points -- if fewer remain, pad with anchors at
      ``20`` and ``59`` C holding the available duty.

    The result is always safe to hand to the firmware or to :func:`interpolate`.
    """
    cleaned: list[tuple[float, int]] = []
    for pair in points:
        try:
            temp_raw, duty_raw = pair
            temp = float(temp_raw)
            duty = int(round(float(duty_raw)))
        except (TypeError, ValueError):
            _LOGGER.warning("skipping invalid curve point: %r", pair)
            continue
        temp = min(max(temp, float(TEMP_MIN)), float(TEMP_MAX))
        duty = min(max(duty, DUTY_MIN), DUTY_MAX)
        cleaned.append((temp, duty))

    # Sort and dedupe by temperature (last value for a given temp wins).
    cleaned.sort(key=lambda p: p[0])
    deduped: dict[float, int] = {}
    for temp, duty in cleaned:
        deduped[temp] = duty
    result: list[tuple[float, int]] = [(t, deduped[t]) for t in sorted(deduped)]

    if len(result) >= 2:
        return result

    # Pad up to two anchor points holding whatever duty we have (or a safe 50).
    fill_duty = result[0][1] if result else 50
    low_anchor = (float(TEMP_MIN + 20), fill_duty)  # 20 C
    high_anchor = (float(CRITICAL_TEMP), fill_duty)  # 59 C
    if not result:
        return [low_anchor, high_anchor]

    existing_temp = result[0][0]
    if existing_temp <= low_anchor[0]:
        return [result[0], high_anchor]
    if existing_temp >= high_anchor[0]:
        return [low_anchor, result[0]]
    return [low_anchor, result[0], high_anchor]


def software_failsafe(
    points_or_duty: int | float | list[tuple[float, int]],
    channel: str,
) -> list[tuple[float, int]]:
    """Build a firmware-native liquid-temp profile for a software-driven channel.

    When a channel's curve is driven by CPU or GPU temperature, the app computes
    a duty each tick and writes a *flat* liquid-temperature profile so the
    firmware holds that duty.  But a flat profile alone is dangerous: if the app
    dies, the firmware would keep holding a possibly-low duty even as the loop
    heats up.  To guard against that we append a hard ramp to 100 % between 48
    and 54 C liquid temperature:

        ``[(20, d), (48, d), (54, 100)]``

    In normal operation the liquid stays well below 45 C, so the firmware always
    serves the computed duty ``d``; only a runaway loop (app dead, heat climbing)
    triggers the 48->54 C ramp to full speed.

    ``points_or_duty`` may be a scalar duty (the common case) or an existing
    point list -- if a list is given, the duty at 20 C (its first/lowest point)
    is used as ``d``.  ``channel`` selects the duty floor: ``"pump"`` is clamped
    to a minimum of 20 % to respect the firmware limit; other channels use 0.
    """
    if isinstance(points_or_duty, (list, tuple)) and not isinstance(points_or_duty, (int, float)):
        validated = validate_points(list(points_or_duty))
        duty = validated[0][1] if validated else 50
    else:
        try:
            duty = int(round(float(points_or_duty)))
        except (TypeError, ValueError):
            _LOGGER.warning("software_failsafe got non-numeric duty %r, using 50", points_or_duty)
            duty = 50

    floor = 20 if channel == "pump" else DUTY_MIN
    duty = min(max(duty, floor), DUTY_MAX)

    return [(20.0, duty), (48.0, duty), (54.0, DUTY_MAX)]


class DutySmoother:
    """Hysteresis to avoid spamming HID writes and to keep fans from yo-yoing.

    Behaviour, evaluated each :meth:`update`:

    * the *first* update always returns ``int(round(target))`` (and records it);
    * when ``target`` exceeds the last applied duty by more than ``deadband``,
      the duty jumps **up immediately** to the target (safety: heat now);
    * when ``target`` is below the last applied duty by more than ``deadband``,
      the duty ramps **down by at most ``max_step_down`` per update** (comfort:
      no sudden fan drops, no oscillation);
    * when ``target`` is within ``deadband`` of the last applied duty,
      :meth:`update` returns ``None`` (nothing to write).

    ``max_step_up`` bounds upward jumps as well; it defaults to ``100`` so an
    upward move is effectively instantaneous, but it can be lowered to soften
    spin-ups.  All returned values are integer percentages.
    """

    def __init__(
        self,
        deadband: float = 2.0,
        max_step_up: int = 100,
        max_step_down: int = 5,
    ) -> None:
        self.deadband: float = float(deadband)
        self.max_step_up: int = int(max_step_up)
        self.max_step_down: int = int(max_step_down)
        self._last_applied: int | None = None

    def update(self, target: float) -> int | None:
        """Return the duty to apply for ``target``, or ``None`` if no change.

        See the class docstring for the full hysteresis semantics.
        """
        target_f = float(target)

        # First sample: apply immediately and record it.
        if self._last_applied is None:
            applied = int(round(target_f))
            self._last_applied = applied
            return applied

        last = self._last_applied
        delta = target_f - last

        # Within the deadband: suppress the write entirely.
        if abs(delta) <= self.deadband:
            return None

        if delta > 0:
            # Jump up, bounded by max_step_up.
            step = min(int(round(delta)), self.max_step_up)
            new_value = last + step
        else:
            # Ramp down by at most max_step_down per tick.
            step = min(int(round(-delta)), self.max_step_down)
            new_value = last - step

        new_value = min(max(new_value, DUTY_MIN), DUTY_MAX)

        # If clamping/stepping produced no net change, there is nothing to write.
        if new_value == last:
            return None

        self._last_applied = new_value
        return new_value

    def reset(self) -> None:
        """Forget the last applied duty so the next update applies immediately."""
        self._last_applied = None
