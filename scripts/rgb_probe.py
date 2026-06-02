#!/usr/bin/env python3
"""Gated hardware validation probe for OpenKraken native LED control.

This script is the **only** sanctioned way to answer the open hardware
questions in ``PROTOCOL.md`` §10 for the NZXT Kraken 2024 Elite RGB
(``1e71:3012``).  It is **interactive and writes real RGB frames to the
attached cooler**, so it must only ever be run by the orchestrator *with
explicit user consent and nothing else holding the device* (no GUI, no
``liquidctl`` CLI, no ``hw_test.py``).

It touches the device **only** through the same vetted choke-point the app
uses (:class:`openkraken.backend.device.KrakenDevice`) and the pure host-side
effect engine (:mod:`openkraken.backend.lighting_fx`).  Every byte it sends
comes from ``PROTOCOL.md`` confirmed sections §3/§4/§6 via
``device.write_lighting_frame`` — this probe never builds wire packets itself
and never emits anything from the §7 do-not-use list.

What it does, in order (each step answers a ``PROTOCOL.md`` §10 item):

  0. connect() + initialize() (cooling/LCD untouched).
  1. query_lighting_info(): dump the raw ``0x20 0x03`` reply hex and the
     parsed :class:`~openkraken.backend.device.LightingInfo`        → §10.3
  2. write a fixed purple frame to the *ring* with apply-variant 0 (OpenRGB
     bytes); ask whether all 24 LEDs lit.  If "no", retry with apply-variant
     1 (liquidctl bytes) and ask again.                            → §10.1 + §10.7
  3. write a fixed purple frame to the *fans* channel the same way. → §10.5
  4. time 10 consecutive ring frame writes to estimate a safe FPS. → §10.6
  5. spectrum-frame burst (3 frames, 1 s apart) to visually confirm per-LED
     addressing around the ring.
  6. ask the user to keep purple or turn the LEDs off, then act.
  7. print a PROBE RESULTS summary block restating every §10 answer.

Convention (so the orchestrator can separate machine output from prompts):

* **stderr**  — all logging (``logging`` at INFO; ``--debug`` for DEBUG).
* **stdout**  — interactive prompts, per-step notes, and the final results
  block.  (We deliberately use ``print``/``input`` here, not the app's
  no-``print`` rule — this is an operator console tool, not app code.)

Run (only when authorised):

    python3 scripts/rgb_probe.py            # interactive
    python3 scripts/rgb_probe.py --yes       # skip the safety gate (orchestrator)
    python3 scripts/rgb_probe.py --debug     # verbose device logging on stderr
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openkraken.backend import lighting_fx
from openkraken.backend.device import KrakenDevice

logger = logging.getLogger("rgb_probe")

# --------------------------------------------------------------------------- #
# Constants for the probe itself (NOT wire bytes — those live in device.py).
# --------------------------------------------------------------------------- #

#: NZXT purple ``#7c3aed`` as an RGB triplet (the app's accent colour).  The
#: device wants GRB on the wire, but ``write_lighting_frame`` does the RGB->GRB
#: conversion for us, so we pass plain RGB everywhere in this script.
_PURPLE_RGB: tuple[int, int, int] = (124, 58, 237)

#: Expected ring LED count per PROTOCOL.md §2 when discovery can't tell us.
_RING_FALLBACK_LEDS = 24
#: Expected fan-chain LED count fallback per INTERFACES-LIGHTING.md.
_FANS_FALLBACK_LEDS = 16

#: Number of consecutive frames to time for the FPS estimate (§10.6).
_FPS_SAMPLE_FRAMES = 10
#: Frames in the spectrum burst, and the gap between them (§5: ~1 FPS ceiling).
_SPECTRUM_FRAMES = 3
_SPECTRUM_GAP_S = 1.0


# --------------------------------------------------------------------------- #
# stdout helpers (operator console — distinct from stderr logging).
# --------------------------------------------------------------------------- #
def out(msg: str = "") -> None:
    """Write a line to stdout (operator-facing)."""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def ask_yes_no(question: str) -> bool:
    """Prompt on stdin for a yes/no answer; default is 'no' (conservative)."""
    while True:
        sys.stdout.write(question + " [y/N]: ")
        sys.stdout.flush()
        try:
            answer = input().strip().lower()
        except EOFError:
            # No interactive stdin: treat as 'no' so we never assume success.
            out("(no input available — assuming NO)")
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        out("Please answer 'y' or 'n'.")


def ask_choice(question: str, options: dict[str, str], default: str) -> str:
    """Prompt for one of ``options`` (key -> label); return the chosen key."""
    keys = "/".join(options)
    while True:
        sys.stdout.write(f"{question} ({keys}) [{default}]: ")
        sys.stdout.flush()
        try:
            answer = input().strip().lower()
        except EOFError:
            out(f"(no input available — defaulting to '{default}')")
            return default
        if answer == "":
            return default
        if answer in options:
            return answer
        out("Choose one of: " + ", ".join(f"{k} ({v})" for k, v in options.items()))


# --------------------------------------------------------------------------- #
# Apply-variant selection.  INTERFACES-LIGHTING.md says device.py holds both
# byte-7 apply variants (0x28 OpenRGB index 0 / 0x08 liquidctl index 1) in a
# module constant ``_APPLY_VARIANTS`` "so the hardware probe can A/B them", and
# exposes the choice as the ``apply_variant`` parameter of
# ``write_lighting_frame``.  The probe drives the A/B directly through that
# public parameter -- no private bytes, no device-side switch needed.
# --------------------------------------------------------------------------- #
def _describe_apply_variant(index: int) -> str:
    """Human label for an apply-variant index (matches PROTOCOL.md §4/§10.1)."""
    if index == 0:
        return "variant 0 (OpenRGB bytes, apply byte[0x07]=0x28)"
    if index == 1:
        return "variant 1 (liquidctl bytes, apply byte[0x07]=0x08)"
    return f"variant {index}"


# --------------------------------------------------------------------------- #
# Frame builders (delegate ALL effect math to lighting_fx — pure, no I/O).
# --------------------------------------------------------------------------- #
def fixed_frame(led_count: int) -> list[tuple[int, int, int]]:
    """A solid full-brightness purple frame of ``led_count`` LEDs."""
    return lighting_fx.frame(
        "fixed", [_PURPLE_RGB], brightness=100, led_count=led_count, t=0.0
    )


def spectrum_frame(led_count: int, t: float) -> list[tuple[int, int, int]]:
    """A spectrum-wave frame at elapsed time ``t`` (per-LED hue around ring)."""
    return lighting_fx.frame(
        "spectrum", [], brightness=100, led_count=led_count, t=t
    )


def off_frame(led_count: int) -> list[tuple[int, int, int]]:
    """An all-black frame (the §4 software 'off')."""
    return lighting_fx.frame("off", [], brightness=100, led_count=led_count, t=0.0)


# --------------------------------------------------------------------------- #
# LED-count resolution from discovery (with documented fallbacks).
# --------------------------------------------------------------------------- #
def resolve_led_count(device: KrakenDevice, channel: str, fallback: int) -> int:
    """Return the detected LED count for ``channel`` (else ``fallback``)."""
    info = getattr(device, "lighting_info", None)
    if info is not None:
        counts = getattr(info, "led_counts", None) or {}
        count = counts.get(channel)
        if isinstance(count, int) and count > 0:
            return count
    logger.warning(
        "No detected LED count for channel %r; using fallback %d", channel, fallback
    )
    return fallback


# --------------------------------------------------------------------------- #
# Probe steps.
# --------------------------------------------------------------------------- #
def probe_channel_solid(
    device: KrakenDevice, channel: str, led_count: int, expected_label: str
) -> dict:
    """Write solid purple to ``channel``, A/B the apply variant on a 'no'.

    Returns a result dict capturing which variant lit all LEDs (or None) and
    the operator's free-text observations, for the §10 summary.
    """
    result: dict = {
        "channel": channel,
        "led_count": led_count,
        "working_variant": None,
        "variant0_all_lit": None,
        "variant1_all_lit": None,
        "variant1_tested": False,
    }
    frame = fixed_frame(led_count)

    # --- apply-variant 0 (OpenRGB) -------------------------------------- §10.1
    out("")
    out(f"--- {channel.upper()} channel: solid purple, "
        f"{_describe_apply_variant(0)} ---")
    out(f"Writing a solid purple frame to {led_count} LED(s) on '{channel}'...")
    if not device.write_lighting_frame(channel, frame, apply_variant=0):
        out(f"!! write_lighting_frame('{channel}', ...) returned False "
            f"(see stderr log). Is the device still connected?")
        logger.error("write_lighting_frame failed on channel %r (variant 0)", channel)
        return result

    lit0 = ask_yes_no(
        f"Did ALL {led_count} {expected_label} light up SOLID PURPLE "
        f"(no dark/last LED, correct colour)?"
    )
    result["variant0_all_lit"] = lit0
    if lit0:
        result["working_variant"] = 0
        out(f"OK: apply-{_describe_apply_variant(0)} lights the whole {channel}.")
        return result

    # --- apply-variant 1 (liquidctl) ------------------------------------ §10.1
    # Re-send the same frame with the liquidctl apply byte (byte[0x07]=0x08) via
    # the public ``apply_variant`` parameter of write_lighting_frame.
    out(f"variant 0 did not fully light '{channel}'. Trying the liquidctl "
        f"apply bytes...")
    result["variant1_tested"] = True
    out(f"Re-writing the same purple frame with {_describe_apply_variant(1)}...")
    if not device.write_lighting_frame(channel, frame, apply_variant=1):
        out(f"!! write_lighting_frame('{channel}', ...) returned False on the "
            f"variant-1 retry (see stderr).")
        logger.error("write_lighting_frame failed on channel %r (variant 1)", channel)
        return result

    lit1 = ask_yes_no(
        f"Now did ALL {led_count} {expected_label} light up SOLID PURPLE?"
    )
    result["variant1_all_lit"] = lit1
    if lit1:
        result["working_variant"] = 1
        out(f"OK: the liquidctl apply bytes ({_describe_apply_variant(1)}) "
            f"work where OpenRGB's did not — note this for PROTOCOL.md §10.1.")
    else:
        out("Neither apply variant fully lit the channel — capture this for the "
            "PROTOCOL.md §10.1 / §10.7 follow-up.")

    return result


def probe_fps(device: KrakenDevice, channel: str, led_count: int) -> dict:
    """Time ``_FPS_SAMPLE_FRAMES`` back-to-back writes; estimate a safe FPS."""
    out("")
    out(f"--- Frame-rate probe: {_FPS_SAMPLE_FRAMES} consecutive '{channel}' "
        f"writes (PROTOCOL.md §10.6) ---")
    out("NOTE: §5/§4828 says the device only reliably accepts ~1 FPS; this "
        "measures how fast the HOST can push frames, not visual smoothness.")
    frame = fixed_frame(led_count)

    durations: list[float] = []
    failures = 0
    for i in range(_FPS_SAMPLE_FRAMES):
        t0 = time.monotonic()
        ok = device.write_lighting_frame(channel, frame)
        dt = time.monotonic() - t0
        durations.append(dt)
        if not ok:
            failures += 1
            logger.error("FPS-probe frame %d/%d failed", i + 1, _FPS_SAMPLE_FRAMES)

    total = sum(durations)
    avg = total / len(durations) if durations else 0.0
    worst = max(durations) if durations else 0.0
    fps = (len(durations) / total) if total > 0 else 0.0

    out(f"Wrote {_FPS_SAMPLE_FRAMES} frames in {total*1000:.1f} ms total: "
        f"avg {avg*1000:.1f} ms/frame, worst {worst*1000:.1f} ms, "
        f"{failures} write failure(s).")
    out(f"Host-side ceiling ~= {fps:.1f} FPS. Keep the engine's lighting "
        f"interval >= 1.0 s anyway (device limit, PROTOCOL.md §5).")
    return {
        "frames": _FPS_SAMPLE_FRAMES,
        "total_s": total,
        "avg_ms": avg * 1000.0,
        "worst_ms": worst * 1000.0,
        "host_fps": fps,
        "failures": failures,
    }


def probe_spectrum(device: KrakenDevice, channel: str, led_count: int) -> dict:
    """Stream a short spectrum burst so the operator can see per-LED addressing."""
    out("")
    out(f"--- Spectrum burst: {_SPECTRUM_FRAMES} frames, {_SPECTRUM_GAP_S:.0f} s "
        f"apart, on '{channel}' ---")
    out("Watch the ring: a spectrum wave should show DIFFERENT hues at "
        "different LED positions (proves per-LED addressing, not a global colour).")
    failures = 0
    base = time.monotonic()
    for i in range(_SPECTRUM_FRAMES):
        t = time.monotonic() - base
        frame = spectrum_frame(led_count, t)
        if not device.write_lighting_frame(channel, frame):
            failures += 1
            logger.error("spectrum frame %d/%d failed", i + 1, _SPECTRUM_FRAMES)
        out(f"  frame {i + 1}/{_SPECTRUM_FRAMES} written (t={t:.1f}s)")
        if i < _SPECTRUM_FRAMES - 1:
            time.sleep(_SPECTRUM_GAP_S)

    per_led = ask_yes_no(
        "Did you see a SPECTRUM (different colours around the ring), confirming "
        "per-LED addressing?"
    )
    return {"frames": _SPECTRUM_FRAMES, "failures": failures, "per_led_ok": per_led}


def finish(device: KrakenDevice, channel: str, led_count: int) -> str:
    """Ask whether to keep purple or turn the LEDs off, and act on it."""
    out("")
    out("--- Finish ---")
    choice = ask_choice(
        "Leave the LEDs on solid purple, or turn them off?",
        {"purple": "keep purple", "off": "LEDs off"},
        default="purple",
    )
    # Always finish on the known-good OpenRGB apply bytes (variant 0).
    if choice == "off":
        out("Writing an all-black frame (software off)...")
        device.write_lighting_frame(channel, off_frame(led_count), apply_variant=0)
    else:
        out("Writing a final solid purple frame...")
        device.write_lighting_frame(channel, fixed_frame(led_count), apply_variant=0)
    out("(Note: LEDs reset on AC power-cycle; the app re-applies on start.)")
    return choice


# --------------------------------------------------------------------------- #
# Lighting-info dump (§10.3).
# --------------------------------------------------------------------------- #
def dump_lighting_info(device: KrakenDevice) -> dict:
    """Run discovery and print the raw ``0x20 0x03`` reply + parsed info."""
    out("")
    out("--- Lighting discovery: raw 0x20 0x03 reply + parsed LightingInfo "
        "(PROTOCOL.md §6 / §10.3) ---")
    info = None
    try:
        info = device.query_lighting_info()
    except Exception:  # pragma: no cover - defensive; device.py should not raise
        logger.exception("query_lighting_info() raised")

    # Raw reply hex, if device.py stashed it for inspection.
    raw = getattr(device, "last_lighting_reply", None)
    if raw is None:
        raw = getattr(device, "_last_lighting_reply", None)
    if raw is not None:
        try:
            hex_str = bytes(raw).hex(" ")
        except Exception:  # pragma: no cover - defensive
            hex_str = repr(raw)
        out("raw 0x20 0x03 reply (hex):")
        out("  " + hex_str)
    else:
        out("raw 0x20 0x03 reply: <not exposed by this device.py build>")

    if info is None:
        out("Parsed LightingInfo: None (discovery failed or unsupported). "
            "Falling back to ring=24, fans=16.")
        return {"channel_count": None, "accessories": {}, "led_counts": {}}

    channel_count = getattr(info, "channel_count", None)
    accessories = dict(getattr(info, "accessories", {}) or {})
    led_counts = dict(getattr(info, "led_counts", {}) or {})
    out(f"channel_count = {channel_count}  (PROTOCOL.md §6: this device should "
        f"report 2 — ring + fan connector)")
    for ch in ("ring", "fans"):
        acc = accessories.get(ch, [])
        acc_hex = ", ".join(f"0x{a:02X}" for a in acc) if acc else "(none)"
        out(f"  {ch:5s}: accessory ids = [{acc_hex}], LED count = "
            f"{led_counts.get(ch, '?')}")
    return {
        "channel_count": channel_count,
        "accessories": accessories,
        "led_counts": led_counts,
    }


# --------------------------------------------------------------------------- #
# Results summary (§10 answers).
# --------------------------------------------------------------------------- #
def print_results(
    info: dict,
    ring: dict,
    fans: dict,
    fps: dict,
    spectrum: dict,
    final_choice: str,
) -> None:
    """Print the PROBE RESULTS block restating every PROTOCOL.md §10 answer."""

    def lit_str(working_variant) -> str:
        if working_variant == 0:
            return "yes (OpenRGB apply byte 0x28)"
        if working_variant == 1:
            return "yes (liquidctl apply byte 0x08)"
        return "NO variant lit the whole channel"

    counts = info.get("led_counts", {})
    out("")
    out("=" * 70)
    out("PROBE RESULTS  (answers PROTOCOL.md §10)")
    out("=" * 70)

    out("§10.1  Which apply variant lights the whole ring:")
    out(f"        ring -> {lit_str(ring.get('working_variant'))}")
    out(f"        (variant0 all-lit={ring.get('variant0_all_lit')}, "
        f"variant1 tested={ring.get('variant1_tested')}, "
        f"variant1 all-lit={ring.get('variant1_all_lit')})")

    out("§10.2  Is the 0x22 0xA0 apply packet required:")
    out("        not isolated by this probe; both writes included the apply "
        "packet. (Visual success above implies apply works; toggling it off "
        "would need a device.py debug hook.)")

    out("§10.3  Real 0x20 0x03 reply contents:")
    out(f"        channel_count={info.get('channel_count')}, "
        f"accessories={info.get('accessories')}, led_counts={counts}")

    out("§10.5  Fan-connector control via the same 0x22 protocol (mask 0x02):")
    fans_count = fans.get("led_count")
    if fans.get("working_variant") is not None:
        out(f"        WORKS — {fans_count} LED(s) lit ({lit_str(fans.get('working_variant'))})")
    elif fans.get("variant0_all_lit") is None:
        out("        not confirmed (write failed or skipped)")
    else:
        out(f"        partial/none lit (variant0={fans.get('variant0_all_lit')}, "
            f"variant1={fans.get('variant1_all_lit')})")

    out("§10.6  Reliable streaming FPS:")
    out(f"        host pushed {fps.get('frames')} frames at "
        f"~{fps.get('host_fps', 0.0):.1f} FPS host-side "
        f"(avg {fps.get('avg_ms', 0.0):.1f} ms, worst {fps.get('worst_ms', 0.0):.1f} ms, "
        f"{fps.get('failures')} failures). Device visual limit stays ~1 FPS (§5).")

    out("§10.7  Quirk A last-LED check (split-buffer fix):")
    v0 = ring.get("variant0_all_lit")
    v1 = ring.get("variant1_all_lit")
    if v0 or v1:
        out("        all 24 ring LEDs lit -> split-buffer 0x22 0x10/0x11 workaround "
            "delivers the last LED. Quirk A resolved on this unit.")
    else:
        out("        NOT all ring LEDs lit -> investigate quirk A / packet split "
            "further (last-LED dark may persist).")

    out("Per-LED addressing (spectrum burst):")
    out(f"        operator confirmed per-LED hues: {spectrum.get('per_led_ok')} "
        f"({spectrum.get('failures')} frame failures)")

    out("Final LED state:")
    out(f"        {'solid purple' if final_choice == 'purple' else 'off (all black)'}")
    out("=" * 70)
    out("Reminder: feed these answers back into PROTOCOL.md §10 and pin the "
        "confirmed apply variant + LED counts in device.py.")


# --------------------------------------------------------------------------- #
# Safety gate + main.
# --------------------------------------------------------------------------- #
def confirm_gate(assume_yes: bool) -> bool:
    """Require explicit consent before writing to the live cooler."""
    out("=" * 70)
    out("OpenKraken — RGB HARDWARE PROBE")
    out("=" * 70)
    out("This will WRITE real RGB frames to the attached NZXT Kraken 2024")
    out("Elite RGB cooler (ring + fan LEDs). Make sure NOTHING else is using")
    out("the device: close the OpenKraken GUI, the liquidctl CLI, and any")
    out("other monitoring tool first. Cooling/pump/LCD are NOT touched.")
    out("")
    if assume_yes:
        out("--yes supplied: proceeding without the interactive gate.")
        return True
    return ask_yes_no("Proceed and write to the LEDs now?")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenKraken RGB hardware probe.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive safety gate (orchestrator use)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="verbose DEBUG device logging on stderr",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not confirm_gate(args.yes):
        out("Aborted by user; no LED writes performed.")
        return 1

    device = KrakenDevice()
    out("")
    out("--- Connecting (this also runs initialize()) ---")
    if not device.connect():
        out("!! connect() failed — see stderr log. Is the cooler attached and "
            "free (nothing else holding the HID handle)?")
        return 2
    out(f"Connected: {device.description} (fw={device.firmware_version or '?'})")

    final_choice = "purple"
    try:
        # 1. discovery dump (§10.3)
        info = dump_lighting_info(device)

        ring_leds = resolve_led_count(device, "ring", _RING_FALLBACK_LEDS)
        fans_leds = resolve_led_count(device, "fans", _FANS_FALLBACK_LEDS)

        # 2. ring solid purple + apply A/B (§10.1 + §10.7)
        ring = probe_channel_solid(
            device, "ring", ring_leds, expected_label="ring LEDs"
        )

        # 3. fans solid purple the same way (§10.5)
        fans = probe_channel_solid(
            device, "fans", fans_leds, expected_label="fan LEDs"
        )

        # 4. FPS timing on the ring (§10.6)
        fps = probe_fps(device, "ring", ring_leds)

        # 5. spectrum burst (per-LED addressing)
        spectrum = probe_spectrum(device, "ring", ring_leds)

        # 6. keep purple / off
        final_choice = finish(device, "ring", ring_leds)

        # 7. summary
        print_results(info, ring, fans, fps, spectrum, final_choice)
    finally:
        device.disconnect()
        out("")
        out("Disconnected. Probe complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
