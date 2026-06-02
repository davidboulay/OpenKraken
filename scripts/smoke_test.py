#!/usr/bin/env python3
"""Offscreen integration smoke test for Kraken CAM.

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


def test_imports() -> None:
    log("[1] importing every krakencam module ...")
    import krakencam  # noqa: F401
    import krakencam.config  # noqa: F401
    import krakencam.backend.device  # noqa: F401
    import krakencam.backend.sensors  # noqa: F401
    import krakencam.backend.curves  # noqa: F401
    import krakencam.backend.lcd_render  # noqa: F401
    import krakencam.backend.engine  # noqa: F401
    import krakencam.gui.theme  # noqa: F401
    import krakencam.gui.widgets.gauge  # noqa: F401
    import krakencam.gui.widgets.graph  # noqa: F401
    import krakencam.gui.widgets.curve_editor  # noqa: F401
    import krakencam.gui.pages.dashboard  # noqa: F401
    import krakencam.gui.pages.cooling  # noqa: F401
    import krakencam.gui.pages.lcd  # noqa: F401
    import krakencam.gui.pages.settings  # noqa: F401
    import krakencam.gui.main_window  # noqa: F401
    import krakencam.app  # noqa: F401
    log("    all imports OK; version=%s" % krakencam.__version__)


def test_config_roundtrip() -> None:
    log("[2] AppConfig defaults -> to_dict -> from_dict round-trip ...")
    from krakencam.config import AppConfig

    cfg = AppConfig()
    d = cfg.to_dict()
    cfg2 = AppConfig.from_dict(d)

    assert cfg2.poll_interval == cfg.poll_interval, "poll_interval mismatch"
    assert cfg2.history_seconds == cfg.history_seconds, "history_seconds mismatch"
    assert cfg2.start_minimized == cfg.start_minimized, "start_minimized mismatch"
    assert cfg2.close_to_tray == cfg.close_to_tray, "close_to_tray mismatch"
    assert cfg2.apply_on_start == cfg.apply_on_start, "apply_on_start mismatch"

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

    # Full dataclass equality (free __eq__).
    assert cfg2 == cfg, "round-tripped AppConfig not equal to original"

    # Tolerant from_dict: garbage in -> defaults out, no raise.
    junk = AppConfig.from_dict({"poll_interval": "nope", "bogus": 1, "pump": "x"})
    assert isinstance(junk.poll_interval, float), "tolerant parse failed"
    log("    config round-trip equal; tolerant parse OK")


def test_curves() -> None:
    log("[3] curves.interpolate / DutySmoother ...")
    from krakencam.backend import curves

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


def test_lcd_render() -> None:
    log("[4] lcd_render.render for all styles (realistic + all-None) ...")
    from krakencam.backend import lcd_render

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
    out = lcd_render.render_to_file("liquid_ring", realistic, "/dev/shm/krakencam_smoke_lcd.png")
    assert Path(out).exists(), "render_to_file did not write file"
    log("    rendered %d styles x 2 datasets, all 640x640 RGB" % len(lcd_render.STYLES))


def test_sensors() -> None:
    log("[5] SystemSensors().read() twice (real read-only sysfs) ...")
    from krakencam.backend.sensors import SystemSensors

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
    from krakencam.config import AppConfig
    from krakencam.backend.device import KrakenDevice, DeviceStatus
    from krakencam.backend.sensors import SystemSensors, SystemSnapshot
    from krakencam.backend.engine import ControlEngine
    from krakencam.gui.main_window import MainWindow
    from krakencam.gui import theme

    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    config = AppConfig()
    device = KrakenDevice()        # constructed, NOT connected
    sensors = SystemSensors()
    engine = ControlEngine(device, sensors, config)   # constructed, NOT started

    win = MainWindow(engine, config)
    win.resize(960, 660)
    win.show()
    app.processEvents()

    # Switch through all 4 sidebar pages.
    for i in range(4):
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

    out = "/tmp/krakencam_window.png"
    pix = win.grab()
    ok = pix.save(out, "PNG")
    assert ok, "failed to grab/save window screenshot"
    log("    window grabbed to %s (%dx%d)" % (out, pix.width(), pix.height()))

    win.close()
    app.processEvents()


def main() -> int:
    test_imports()
    test_config_roundtrip()
    test_curves()
    test_lcd_render()
    test_sensors()
    test_gui()
    log("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
