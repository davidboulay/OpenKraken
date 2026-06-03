#!/usr/bin/env python3
"""Offscreen integration smoke test for OpenKraken.

Exercises every module WITHOUT touching hardware: no device.connect(), no
engine.start(), no hidraw, no liquidctl CLI. Reading real sysfs/proc for the
SystemSensors test is read-only and safe.

Run with:  QT_QPA_PLATFORM=offscreen python3 scripts/smoke_test.py
Exits 0 on success; raises (non-zero) on the first assertion failure.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make the package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


#: Count of tests that were skipped because a not-yet-integrated helper was
#: missing.  These are warnings, not failures, so the suite stays usable while
#: the GNOME-compatibility work lands across app.py / main_window.py / config.py.
_SKIPPED: list[str] = []


def skip(reason: str) -> None:
    """Record a soft skip (a missing in-progress helper) without failing."""
    _SKIPPED.append(reason)
    sys.stdout.write("    \033[1;33mSKIP\033[0m %s\n" % reason)
    sys.stdout.flush()


def test_imports() -> None:
    log("[1] importing every openkraken module ...")
    import openkraken  # noqa: F401
    import openkraken.config  # noqa: F401
    import openkraken.backend.device  # noqa: F401
    import openkraken.backend.sensors  # noqa: F401
    import openkraken.backend.curves  # noqa: F401
    import openkraken.backend.lcd_render  # noqa: F401
    import openkraken.backend.engine  # noqa: F401
    import openkraken.gui.theme  # noqa: F401
    import openkraken.gui.widgets.gauge  # noqa: F401
    import openkraken.gui.widgets.graph  # noqa: F401
    import openkraken.gui.widgets.curve_editor  # noqa: F401
    import openkraken.backend.lighting_fx  # noqa: F401
    import openkraken.gui.pages.dashboard  # noqa: F401
    import openkraken.gui.pages.cooling  # noqa: F401
    import openkraken.gui.pages.lcd  # noqa: F401
    import openkraken.gui.pages.lighting  # noqa: F401
    import openkraken.gui.pages.settings  # noqa: F401
    import openkraken.gui.main_window  # noqa: F401
    import openkraken.app  # noqa: F401
    log("    all imports OK; version=%s" % openkraken.__version__)


def test_config_roundtrip() -> None:
    log("[2] AppConfig defaults -> to_dict -> from_dict round-trip ...")
    from openkraken.config import AppConfig

    cfg = AppConfig()
    d = cfg.to_dict()
    cfg2 = AppConfig.from_dict(d)

    assert cfg2.poll_interval == cfg.poll_interval, "poll_interval mismatch"
    assert cfg2.history_seconds == cfg.history_seconds, "history_seconds mismatch"
    assert cfg2.start_minimized == cfg.start_minimized, "start_minimized mismatch"
    assert cfg2.close_to_tray == cfg.close_to_tray, "close_to_tray mismatch"
    assert cfg2.apply_on_start == cfg.apply_on_start, "apply_on_start mismatch"

    # --- run_in_background (GNOME / no-tray background mode) ------------------
    # Field added by the sibling agent's config.py change; skip-but-warn until it
    # lands so the suite stays usable mid-integration.
    if hasattr(cfg, "run_in_background"):
        assert cfg.run_in_background is True, "run_in_background must default to True"
        assert (
            cfg2.run_in_background == cfg.run_in_background
        ), "run_in_background round-trip mismatch"
        # It must be serialized so it actually persists, and reload as a bool.
        assert "run_in_background" in d, "run_in_background not in to_dict() output"
        # A non-default value (and a tolerant-parse garbage value) must survive.
        flipped = AppConfig.from_dict({"run_in_background": False})
        assert flipped.run_in_background is False, "run_in_background=False did not round-trip"
        tolerant = AppConfig.from_dict({"run_in_background": "nope"})
        assert isinstance(
            tolerant.run_in_background, bool
        ), "tolerant run_in_background parse did not yield a bool"
        log("    run_in_background present (default True) and round-trips")
    else:
        skip("AppConfig.run_in_background not present yet (config.py sibling change)")

    assert cfg2.pump.mode == cfg.pump.mode, "pump.mode mismatch"
    assert cfg2.pump.source == cfg.pump.source, "pump.source mismatch"
    assert cfg2.pump.profile == cfg.pump.profile, "pump.profile mismatch"
    assert cfg2.pump.points == cfg.pump.points, (
        "pump.points mismatch: %r != %r" % (cfg2.pump.points, cfg.pump.points)
    )
    assert cfg2.fan.points == cfg.fan.points, "fan.points mismatch"

    assert cfg2.lcd.mode == cfg.lcd.mode, "lcd.mode mismatch"
    assert cfg2.lcd.brightness == cfg.lcd.brightness, "lcd.brightness mismatch"
    assert cfg2.lcd.orientation == cfg.lcd.orientation, "lcd.orientation mismatch"
    assert cfg2.lcd.sensor_style == cfg.lcd.sensor_style, "lcd.sensor_style mismatch"

    # --- Lighting config round-trip (INTERFACES-LIGHTING.md) -----------------
    assert cfg2.lighting.enabled == cfg.lighting.enabled, "lighting.enabled mismatch"
    assert cfg.lighting.enabled is False, "lighting must default to disabled"
    assert cfg2.lighting.sync == cfg.lighting.sync, "lighting.sync mismatch"
    for ch in ("ring", "fans"):
        a = getattr(cfg.lighting, ch)
        b = getattr(cfg2.lighting, ch)
        assert b.mode == a.mode, "lighting.%s.mode mismatch" % ch
        assert b.brightness == a.brightness, "lighting.%s.brightness mismatch" % ch
        assert b.speed == a.speed, "lighting.%s.speed mismatch" % ch
        # Colours must survive the list<->tuple JSON round-trip as tuples.
        assert b.colors == a.colors, (
            "lighting.%s.colors mismatch: %r != %r" % (ch, b.colors, a.colors)
        )
        assert all(isinstance(c, tuple) and len(c) == 3 for c in b.colors), (
            "lighting.%s.colors not normalized to RGB tuples: %r" % (ch, b.colors)
        )

    # Full dataclass equality (free __eq__) — also covers lighting structurally.
    assert cfg2 == cfg, "round-tripped AppConfig not equal to original"

    # A non-default lighting config must survive the round-trip exactly.
    from openkraken.config import LightingConfig, LightingChannelConfig

    custom = AppConfig()
    custom.lighting = LightingConfig(
        enabled=True,
        sync=False,
        ring=LightingChannelConfig(
            mode="cycle",
            colors=[(10, 20, 30), (200, 100, 50)],
            brightness=70,
            speed="fast",
        ),
        fans=LightingChannelConfig(
            mode="fixed",
            colors=[(255, 0, 0)],
            brightness=40,
            speed="slow",
        ),
    )
    custom2 = AppConfig.from_dict(custom.to_dict())
    assert custom2.lighting == custom.lighting, (
        "custom lighting round-trip mismatch: %r != %r"
        % (custom2.lighting, custom.lighting)
    )

    # Swatch-less modes (off/spectrum, max_colors=0) persist an EMPTY colour list
    # and must reload as [] (not the fallback purple) — round-trip identity.
    empty_cols = AppConfig()
    empty_cols.lighting = LightingConfig(
        enabled=True,
        sync=False,
        ring=LightingChannelConfig(mode="spectrum", colors=[], brightness=100, speed="normal"),
        fans=LightingChannelConfig(mode="off", colors=[], brightness=100, speed="normal"),
    )
    empty2 = AppConfig.from_dict(empty_cols.to_dict())
    assert empty2.lighting.ring.colors == [], (
        "spectrum empty colours did not round-trip to []: %r" % empty2.lighting.ring.colors
    )
    assert empty2.lighting.fans.colors == [], (
        "off empty colours did not round-trip to []: %r" % empty2.lighting.fans.colors
    )
    assert empty2.lighting == empty_cols.lighting, (
        "empty-colour lighting round-trip mismatch: %r != %r"
        % (empty2.lighting, empty_cols.lighting)
    )

    # Tolerant from_dict: garbage in -> defaults out, no raise.
    junk = AppConfig.from_dict({"poll_interval": "nope", "bogus": 1, "pump": "x"})
    assert isinstance(junk.poll_interval, float), "tolerant parse failed"
    # Garbage lighting block must fall back to the (disabled) default, not raise.
    junk2 = AppConfig.from_dict({"lighting": "nope"})
    assert junk2.lighting.enabled is False, "tolerant lighting parse failed"
    log("    config round-trip equal; lighting + tolerant parse OK")


def _pump(app, ms: int = 500) -> None:
    """Spin the Qt event loop for ``ms`` milliseconds (deliver queued slots)."""
    from PyQt6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_single_instance() -> None:
    log("[*] app.setup_single_instance: server + local-socket activate round-trip ...")
    from openkraken import app as appmod

    if not hasattr(appmod, "setup_single_instance"):
        skip("app.setup_single_instance not present yet (app.py sibling change)")
        return

    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    # A unique per-run name so this test never collides with a real running
    # instance (the production name is f"openkraken-{os.getuid()}").
    name = "openkraken-smoke-%d-%d" % (os.getuid(), os.getpid())
    # Make sure no stale socket from a previous aborted run lingers.
    QLocalServer.removeServer(name)

    activated: list[str] = []

    def on_activate() -> None:
        activated.append("activate")

    server = appmod.setup_single_instance(name, on_activate)
    assert server is not None, "setup_single_instance returned None for a free name"

    try:
        # 1) "activate" line -> the registered callback fires (this is what a
        #    second invocation does to raise the running window).
        client = QLocalSocket()
        client.connectToServer(name)
        assert client.waitForConnected(1000), (
            "client could not connect to the single-instance server"
        )
        client.write(b"activate\n")
        assert client.waitForBytesWritten(1000), "client write did not flush"
        _pump(app)
        client.disconnectFromServer()
        assert activated == ["activate"], (
            "on_activate callback did not fire on 'activate' line: %r" % activated
        )

        # 2) "ping" line must NOT raise the window (autostart --minimized double
        #    fire only signals presence) -> callback count unchanged.
        client2 = QLocalSocket()
        client2.connectToServer(name)
        assert client2.waitForConnected(1000), "second client could not connect"
        client2.write(b"ping\n")
        assert client2.waitForBytesWritten(1000), "ping write did not flush"
        _pump(app)
        client2.disconnectFromServer()
        assert activated == ["activate"], (
            "'ping' must not trigger on_activate: %r" % activated
        )

        # 3) The connect-probe main() uses to detect a running instance:
        #    _notify_running_instance returns True while the server is up and
        #    delivers the message. ('activate' here would fire the callback
        #    again, so use the presence-only ping to keep the count stable.)
        if hasattr(appmod, "_notify_running_instance"):
            notified = appmod._notify_running_instance(name, activate=False)
            _pump(app)
            assert notified is True, (
                "_notify_running_instance should detect the live server"
            )
            assert activated == ["activate"], (
                "ping via _notify_running_instance must not fire on_activate: %r"
                % activated
            )
        else:
            skip("app._notify_running_instance not present (probe path untested)")

        # 4) Live-collision protection: while THIS server owns the name, a second
        #    setup_single_instance(name) on the same live name must NOT steal it.
        #    (An unconditional removeServer would unlink the live socket and let a
        #    second server listen, leaving two unreachable "primaries".)
        second = appmod.setup_single_instance(name, lambda: activated.append("second"))
        assert second is None, (
            "a second setup_single_instance stole a LIVE socket name (two primaries)"
        )
        # The original server must still be reachable after the rejected collision.
        client3 = QLocalSocket()
        client3.connectToServer(name)
        assert client3.waitForConnected(1000), (
            "original server unreachable after a rejected second instance"
        )
        client3.write(b"activate\n")
        assert client3.waitForBytesWritten(1000), "post-collision write did not flush"
        _pump(app)
        client3.disconnectFromServer()
        assert activated == ["activate", "activate"], (
            "original server did not handle activate after collision: %r" % activated
        )

        log(
            "    single-instance: activate fires, ping ignored, probe detects "
            "server, live collision rejected (no second primary)"
        )
    finally:
        # Release the socket name regardless of outcome.
        try:
            server.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
        QLocalServer.removeServer(name)

    # 5) With nothing listening, the probe must report "no running instance"
    #    (False) so a fresh launch becomes the primary.
    if hasattr(appmod, "_notify_running_instance"):
        assert appmod._notify_running_instance(name, activate=True) is False, (
            "_notify_running_instance should be False when no server owns the name"
        )

    # 6) Stale-socket recovery: a leftover socket FILE (no live listener, as after
    #    a crash) must not block a fresh primary. setup_single_instance should
    #    detect nobody is listening and reclaim the name.
    stale_name = "openkraken-smoke-stale-%d-%d" % (os.getuid(), os.getpid())
    QLocalServer.removeServer(stale_name)
    crashed = QLocalServer()
    assert crashed.listen(stale_name), "could not create the would-be-stale server"
    # Simulate a crash: drop the listener without removeServer so the socket file
    # is orphaned (Qt may leave the on-disk node behind).
    crashed.setParent(None)
    del crashed
    app.processEvents()
    recovered = appmod.setup_single_instance(stale_name, lambda: None)
    try:
        assert recovered is not None, (
            "setup_single_instance failed to recover from a stale socket file"
        )
        assert recovered.isListening(), "recovered server is not listening"
    finally:
        if recovered is not None:
            recovered.close()
        QLocalServer.removeServer(stale_name)


def test_close_action_matrix() -> None:
    log("[*] MainWindow._close_action: tray / background / quit decision matrix ...")
    from PyQt6.QtWidgets import QApplication
    from openkraken.config import AppConfig
    from openkraken.backend.device import KrakenDevice
    from openkraken.backend.sensors import SystemSensors
    from openkraken.backend.engine import ControlEngine
    from openkraken.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)

    config = AppConfig()
    config.check_updates_on_start = False  # no network in tests
    device = KrakenDevice()  # constructed, NOT connected
    sensors = SystemSensors()
    engine = ControlEngine(device, sensors, config)  # constructed, NOT started

    win = MainWindow(engine, config)

    if not hasattr(win, "_close_action"):
        skip("MainWindow._close_action not present yet (main_window.py sibling change)")
        # Do NOT route teardown through win.close()/closeEvent here: closeEvent
        # may depend on the very helper we just found missing. Drop the window
        # without firing its close path.
        win.deleteLater()
        app.processEvents()
        return

    # run_in_background may not exist yet on the config; default to True (its
    # spec'd default) so the matrix is meaningful even mid-integration.
    has_rib = hasattr(config, "run_in_background")

    def set_rib(value: bool) -> None:
        if has_rib:
            config.run_in_background = value

    # Force "no tray" regardless of the real session: the decision must not
    # depend on a tray host actually existing under the test harness.  The
    # window owns its tray reference as ``_tray`` (see closeEvent), so a None
    # there means "no tray" to _close_action.
    win._tray = None

    # 1) No tray + run_in_background -> "background".
    set_rib(True)
    action = win._close_action()
    if has_rib:
        assert action == "background", (
            "no-tray + run_in_background should be 'background', got %r" % action
        )

    # 2) No tray + NOT run_in_background -> "quit".
    set_rib(False)
    action = win._close_action()
    if has_rib:
        assert action == "quit", (
            "no-tray + run_in_background=False should be 'quit', got %r" % action
        )

    # The returned value must always be one of the three documented tokens,
    # whatever the tray/config state.
    assert win._close_action() in ("tray", "background", "quit"), (
        "_close_action returned an undocumented token: %r" % win._close_action()
    )

    # Restore the default before tearing down.
    set_rib(True)
    log("    _close_action matrix OK (no-tray -> background/quit by config)")

    win.close()
    app.processEvents()


def test_curves() -> None:
    log("[3] curves.interpolate / DutySmoother ...")
    from openkraken.backend import curves

    # Empty list -> 50.0 per spec.
    assert curves.interpolate([], 40.0) == 50.0, "empty interpolate != 50.0"
    # Single point -> that duty regardless of x.
    assert curves.interpolate([(30, 70)], 99.0) == 70.0, "single-point interpolate"
    assert curves.interpolate([(30, 70)], 0.0) == 70.0, "single-point interpolate low"
    # Below/above range -> clamp to end duties.
    pts = [(20, 60), (40, 100)]
    assert curves.interpolate(pts, 10.0) == 60.0, "below-range clamp"
    assert curves.interpolate(pts, 99.0) == 100.0, "above-range clamp"
    mid = curves.interpolate(pts, 30.0)
    assert 79.0 <= mid <= 81.0, "midpoint interpolation off: %r" % mid

    vp = curves.validate_points([(40, 200), (20, -5), (40, 50)])
    assert len(vp) >= 2, "validate_points must yield >= 2 points"
    assert all(0 <= d <= 100 for _, d in vp), "duty not clamped"
    assert vp == sorted(vp), "points not sorted by temp"

    fs = curves.software_failsafe(60, "pump")
    assert any(d == 100 for _, d in fs), "failsafe missing 100% anchor"

    sm = curves.DutySmoother(deadband=2.0, max_step_up=100, max_step_down=5)
    first = sm.update(60.0)
    assert first == 60, "first update should apply target: %r" % first
    assert sm.update(61.0) is None, "within deadband should return None"
    jump = sm.update(90.0)
    assert jump is not None and jump > 60, "up-jump should apply: %r" % jump
    down = sm.update(0.0)
    assert down is not None and (jump - down) <= 5, "down-ramp must be bounded: %r->%r" % (jump, down)
    sm.reset()
    assert sm.update(42.0) == 42, "after reset first update applies target"
    log("    curves + smoother behave per spec")


def test_lighting_fx() -> None:
    log("[*] lighting_fx.frame for every mode (length / black / determinism) ...")
    from openkraken.backend import lighting_fx

    led_count = 24                       # the ring; PROTOCOL.md §2
    palette = [(124, 58, 237), (255, 0, 0), (0, 200, 80), (10, 10, 240)]

    assert lighting_fx.MODES, "lighting_fx.MODES is empty"

    def colors_for(spec) -> list:
        """A color list satisfying the mode's min/max_colors bounds."""
        n = max(spec.min_colors, min(spec.max_colors, len(palette)))
        return palette[:n]

    for key, spec in lighting_fx.MODES.items():
        assert spec.key == key, "MODES key %r != spec.key %r" % (key, spec.key)
        cols = colors_for(spec)

        # --- exact length at full brightness, at a few times --------------
        for t in (0.0, 1.0, 3.7, 60.0):
            frame = lighting_fx.frame(key, cols, 100, led_count, t)
            assert len(frame) == led_count, (
                "%s@t=%s wrong length: %d != %d" % (key, t, len(frame), led_count)
            )
            for px in frame:
                assert isinstance(px, tuple) and len(px) == 3, (
                    "%s pixel not RGB triplet: %r" % (key, px)
                )
                assert all(isinstance(c, int) and 0 <= c <= 255 for c in px), (
                    "%s pixel out of 0-255 range: %r" % (key, px)
                )

        # --- brightness 0 -> all black, at several times ------------------
        for t in (0.0, 2.5, 11.0):
            black = lighting_fx.frame(key, cols, 0, led_count, t)
            assert len(black) == led_count, "%s black wrong length" % key
            assert all(px == (0, 0, 0) for px in black), (
                "%s@t=%s brightness=0 not all black: %r" % (key, t, black[:3])
            )

        # --- determinism: same inputs (fixed t) -> identical frame --------
        f1 = lighting_fx.frame(key, cols, 100, led_count, 4.2)
        f2 = lighting_fx.frame(key, cols, 100, led_count, 4.2)
        assert f1 == f2, "%s not deterministic for fixed t" % key

        # --- off is always black; fixed is solid & matches its color ------
        if key == "off":
            assert all(px == (0, 0, 0) for px in f1), "off mode not all black"
        if key == "fixed":
            solid = lighting_fx.frame("fixed", [(124, 58, 237)], 100, led_count, 9.0)
            assert len(set(solid)) == 1, "fixed mode not uniform: %r" % set(solid)
            assert solid[0] == (124, 58, 237), (
                "fixed mode color not preserved at 100%%: %r" % (solid[0],)
            )

        # --- animated modes actually change over time ---------------------
        if spec.animated:
            early = lighting_fx.frame(key, cols, 100, led_count, 0.0)
            late = lighting_fx.frame(key, cols, 100, led_count, 3.0)
            assert early != late, (
                "%s flagged animated but frame did not change over time" % key
            )

    # A 16-LED fan chain must also produce a correctly sized frame.
    f16 = lighting_fx.frame("fixed", [(124, 58, 237)], 100, 16, 0.0)
    assert len(f16) == 16, "fixed frame wrong length for 16-LED fan chain"
    log("    %d modes validated (len/black/determinism/animation)" % len(lighting_fx.MODES))


def test_device_parse_lighting_info() -> None:
    log("[*] device._parse_lighting_info: synthetic 0x20 0x03 reply (PROTOCOL.md §6) ...")
    from openkraken.backend import device as devmod

    # Build a synthetic 64-byte 0x21 0x03 reply:
    #   byte 14 = channel_count (2)
    #   byte 15 = ring accessory 0x1E (Kraken Elite ring -> 24 LEDs)
    #   bytes 21,22 = fan accessories 0x17 + 0x18 (two 8-LED RGB Core fans -> 16)
    reply = [0x00] * 64
    reply[0], reply[1] = 0x21, 0x03
    reply[14] = 2
    reply[15] = 0x1E          # ring base = 15 + 0*6
    reply[21] = 0x17          # fans base = 15 + 1*6 = 21
    reply[22] = 0x18

    info = devmod._parse_lighting_info(reply)
    assert info is not None, "parser returned None for a valid reply"
    assert info.channel_count == 2, "channel_count != 2: %r" % info.channel_count
    assert info.led_counts["ring"] == 24, "ring LED count != 24: %r" % info.led_counts
    assert info.led_counts["fans"] == 16, "fans LED count != 16: %r" % info.led_counts
    assert info.accessories["ring"] == [0x1E], "ring accessories: %r" % info.accessories
    assert info.accessories["fans"] == [0x17, 0x18], (
        "fans accessories: %r" % info.accessories
    )

    # Unknown accessory id -> fallback 8 LEDs, no raise.
    reply_unknown = [0x00] * 64
    reply_unknown[0], reply_unknown[1] = 0x21, 0x03
    reply_unknown[14] = 2
    reply_unknown[15] = 0x99          # unknown ring accessory -> fallback 8
    info2 = devmod._parse_lighting_info(reply_unknown)
    assert info2 is not None, "parser returned None for unknown-id reply"
    assert info2.led_counts["ring"] == devmod._FALLBACK_ACCESSORY_LEDS, (
        "unknown id did not fall back to %d: %r"
        % (devmod._FALLBACK_ACCESSORY_LEDS, info2.led_counts)
    )
    # A channel with no accessories falls back to the conservative default.
    assert info2.led_counts["fans"] == devmod._FALLBACK_LED_COUNTS["fans"], (
        "empty fan channel did not use the fallback: %r" % info2.led_counts
    )

    # Reply too short to parse -> None (validation, not a crash).
    assert devmod._parse_lighting_info([0x21, 0x03]) is None, (
        "short reply did not return None"
    )
    log("    parser: ring=24 fans=16, unknown-id + short-reply paths OK")


class _FakeLightingDevice:
    """No-hardware stand-in exposing only what the engine's lighting path uses."""

    def __init__(self) -> None:
        from openkraken.backend.device import LightingInfo

        self.is_connected = True
        self.lighting_info = LightingInfo(
            channel_count=2,
            accessories={"ring": [0x1E], "fans": [0x17, 0x18]},
            led_counts={"ring": 24, "fans": 16},
        )
        # (channel, frame_len) recorded per write so the test can assert framing.
        self.writes: list[tuple[str, int]] = []

    def write_lighting_frame(self, channel, led_colors, apply_variant=0):
        self.writes.append((channel, len(led_colors)))
        return True


def test_engine_apply_lighting() -> None:
    log("[*] engine._do_apply_lighting against a fake device (no hardware) ...")
    from openkraken.config import AppConfig, LightingConfig, LightingChannelConfig
    from openkraken.backend.engine import ControlEngine
    from openkraken.backend.sensors import SystemSensors

    config = AppConfig()
    fake = _FakeLightingDevice()
    engine = ControlEngine(fake, SystemSensors(), config)  # constructed, NOT started

    # Enabled animated config, sync OFF so both channels use their own config.
    cfg = LightingConfig(
        enabled=True,
        sync=False,
        ring=LightingChannelConfig(
            mode="spectrum", colors=[], brightness=100, speed="normal"
        ),
        fans=LightingChannelConfig(
            mode="cycle", colors=[(10, 20, 30), (200, 100, 50)],
            brightness=80, speed="fast",
        ),
    )
    engine._do_apply_lighting(cfg)

    # One initial frame per channel, sized to the detected LED counts.
    assert ("ring", 24) in fake.writes, "ring not written at 24 LEDs: %r" % fake.writes
    assert ("fans", 16) in fake.writes, "fans not written at 16 LEDs: %r" % fake.writes
    assert len(fake.writes) == 2, "expected exactly 2 apply-time writes: %r" % fake.writes

    # Animated state armed: both channels marked animated, origin set, and
    # last_write advanced to the apply-time write (NOT left at the 0.0 sentinel),
    # so the next streamed frame respects the >=1 s spacing.
    for ch in ("ring", "fans"):
        state = engine._lighting[ch]
        assert state.animated, "%s not flagged animated" % ch
        assert state.origin > 0.0, "%s origin not set" % ch
        assert state.last_write > 0.0, (
            "%s last_write left at sentinel; first streamed frame would breach "
            "the ~1 FPS ceiling" % ch
        )

    # Disabled config must NEVER write to the LEDs.
    fake.writes.clear()
    engine._do_apply_lighting(LightingConfig(enabled=False))
    assert fake.writes == [], "disabled lighting still wrote frames: %r" % fake.writes
    log("    apply wrote ring=24/fans=16, armed state, and respected enabled=False")


def test_lcd_render() -> None:
    log("[4] lcd_render.render for all styles (realistic + all-None) ...")
    from openkraken.backend import lcd_render

    realistic = lcd_render.LcdData(
        liquid_temp=41.3,
        cpu_temp=68.0,
        cpu_load=44.0,
        gpu_temp=59.0,
        gpu_load=23.0,
        pump_rpm=1860,
        fan_rpm=983,
    )
    empty = lcd_render.LcdData(None, None, None, None, None, None, None)

    for style in lcd_render.STYLES:
        for data, tag in ((realistic, "realistic"), (empty, "none")):
            img = lcd_render.render(style, data)
            assert img.size == (640, 640), "%s/%s wrong size: %r" % (style, tag, img.size)
            assert img.mode == "RGB", "%s/%s wrong mode: %r" % (style, tag, img.mode)
    # render_to_file to tmpfs.
    out = lcd_render.render_to_file("liquid_ring", realistic, "/dev/shm/openkraken_smoke_lcd.png")
    assert Path(out).exists(), "render_to_file did not write file"
    log("    rendered %d styles x 2 datasets, all 640x640 RGB" % len(lcd_render.STYLES))


def test_sensors() -> None:
    log("[5] SystemSensors().read() twice (real read-only sysfs) ...")
    from openkraken.backend.sensors import SystemSensors

    s = SystemSensors()
    snap1 = s.read()
    time.sleep(0.25)
    snap2 = s.read()
    log("    snap1: cpu_temp=%s cpu_load=%s gpu_temp=%s gpu_load=%s ram=%s/%s"
        % (snap1.cpu_temp, snap1.cpu_load, snap1.gpu_temp, snap1.gpu_load,
           snap1.ram_used_gb, snap1.ram_total_gb))
    log("    snap2: cpu_temp=%s cpu_load=%s gpu_temp=%s gpu_load=%s freq=%s"
        % (snap2.cpu_temp, snap2.cpu_load, snap2.gpu_temp, snap2.gpu_load,
           snap2.cpu_freq_mhz))


def test_gui() -> None:
    log("[6] QApplication + KrakenDevice + SystemSensors + ControlEngine + MainWindow ...")
    from PyQt6.QtWidgets import QApplication
    from openkraken.config import AppConfig
    from openkraken.backend.device import KrakenDevice, DeviceStatus
    from openkraken.backend.sensors import SystemSensors, SystemSnapshot
    from openkraken.backend.engine import ControlEngine
    from openkraken.gui.main_window import MainWindow
    from openkraken.gui import theme

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    config = AppConfig()
    config.check_updates_on_start = False  # no network in tests
    device = KrakenDevice()        # constructed, NOT connected
    sensors = SystemSensors()
    engine = ControlEngine(device, sensors, config)   # constructed, NOT started

    win = MainWindow(engine, config)
    win.resize(960, 660)
    win.show()
    app.processEvents()

    # Switch through every sidebar page (Lighting now sits between Cooling and
    # LCD, so the count is no longer hard-coded to 4).
    nav_count = len(win._nav_buttons)
    assert nav_count >= 5, "expected >=5 nav pages (incl. Lighting), got %d" % nav_count
    for i in range(nav_count):
        win._nav_buttons[i].click()
        app.processEvents()
        assert win._stack.currentIndex() == i, "nav %d did not switch stack" % i

    # Feed a fake sample to every page that listens.
    status = DeviceStatus(
        liquid_temp=41.0, pump_rpm=1860, pump_duty=60,
        fan_rpm=983, fan_duty=40, connected=True, timestamp=time.monotonic(),
    )
    snap = SystemSnapshot(
        cpu_temp=68.0, cpu_load=44.0, cpu_freq_mhz=4200.0,
        gpu_temp=59.0, gpu_load=23.0, gpu_vram_used_mb=166.0,
        gpu_vram_total_mb=512.0, gpu_power_w=24.0,
        ram_used_gb=12.0, ram_total_gb=32.0, timestamp=time.time(),
    )
    # Emit through the engine signal so each page's connected slot fires.
    engine.sample_ready.emit(status, snap)
    engine.connection_changed.emit(True, "NZXT Kraken 2024 Elite RGB")
    app.processEvents()

    # Also drive a disconnected/None sample to exercise the None paths.
    engine.sample_ready.emit(DeviceStatus.disconnected(), snap)
    engine.connection_changed.emit(False, "")
    app.processEvents()

    # Restore connected state and feed a short burst of samples so the
    # history-backed graphs render real polylines in the screenshot.
    engine.connection_changed.emit(True, "NZXT Kraken 2024 Elite RGB")
    import math
    base = time.monotonic()
    for k in range(60):
        wob = math.sin(k / 6.0)
        st = DeviceStatus(
            liquid_temp=40.0 + 3.0 * wob,
            pump_rpm=1800 + int(120 * wob),
            pump_duty=60,
            fan_rpm=950 + int(180 * wob),
            fan_duty=40,
            connected=True,
            timestamp=base + k,
        )
        sn = SystemSnapshot(
            cpu_temp=62.0 + 8.0 * wob, cpu_load=40.0 + 20.0 * wob,
            cpu_freq_mhz=4200.0, gpu_temp=55.0 + 6.0 * wob,
            gpu_load=20.0 + 10.0 * wob, gpu_vram_used_mb=166.0,
            gpu_vram_total_mb=512.0, gpu_power_w=24.0,
            ram_used_gb=12.0, ram_total_gb=32.0, timestamp=time.time(),
        )
        engine.history.append(st, sn)
    engine.sample_ready.emit(status, snap)
    win._nav_buttons[0].click()  # back to dashboard
    app.processEvents()
    app.processEvents()

    out = "/tmp/openkraken_window.png"
    pix = win.grab()
    ok = pix.save(out, "PNG")
    assert ok, "failed to grab/save window screenshot"
    log("    window grabbed to %s (%dx%d)" % (out, pix.width(), pix.height()))

    win.close()
    app.processEvents()


def test_lighting_page() -> None:
    log("[*] LightingPage construction + offscreen preview paint ...")
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QPixmap, QPainter
    from PyQt6.QtWidgets import QApplication
    from openkraken.config import AppConfig
    from openkraken.backend.device import KrakenDevice
    from openkraken.backend.sensors import SystemSensors
    from openkraken.backend.engine import ControlEngine
    from openkraken.gui.pages.lighting import LightingPage
    from openkraken.gui import theme

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    config = AppConfig()
    config.check_updates_on_start = False  # no network in tests
    device = KrakenDevice()        # constructed, NOT connected
    sensors = SystemSensors()
    engine = ControlEngine(device, sensors, config)   # constructed, NOT started

    # Same constructor signature as every other page (engine, config, parent).
    page = LightingPage(engine, config)
    page.resize(QSize(720, 560))
    page.show()
    app.processEvents()

    # Force at least one full layout/paint pass of the page (including the
    # round ring preview widget) by grabbing it to a pixmap offscreen.
    pix = page.grab()
    assert not pix.isNull(), "LightingPage grab produced a null pixmap"
    assert pix.width() > 0 and pix.height() > 0, "LightingPage grabbed empty"

    # Explicitly paint into our own pixmap as well, to exercise the page's
    # paint path once more without needing a visible window/compositor.
    canvas = QPixmap(page.size())
    canvas.fill()
    painter = QPainter(canvas)
    page.render(painter)
    painter.end()
    assert not canvas.isNull(), "LightingPage render to pixmap failed"

    page.close()
    app.processEvents()
    log("    LightingPage built and painted once offscreen (%dx%d)"
        % (pix.width(), pix.height()))


def main() -> int:
    test_imports()
    test_config_roundtrip()
    test_single_instance()
    test_close_action_matrix()
    test_curves()
    test_lighting_fx()
    test_device_parse_lighting_info()
    test_engine_apply_lighting()
    test_lcd_render()
    test_sensors()
    test_gui()
    test_lighting_page()
    if _SKIPPED:
        log("\nALL SMOKE TESTS PASSED (with %d skipped, pending integration):" % len(_SKIPPED))
        for reason in _SKIPPED:
            log("    - %s" % reason)
    else:
        log("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
