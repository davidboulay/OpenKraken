"""Live hardware integration test for OpenKraken.

Run ONLY when no other process (liquidctl CLI, the GUI app) is using the
cooler.  Exercises every device path the app uses, restoring previous
state where possible:

  1. connect + initialize (firmware, LCD brightness/orientation)
  2. get_status() x2
  3. LCD brightness round-trip (30% -> original)
  4. render a live sensor frame and time the static-image upload
  5. restore the firmware liquid-temp screen
  6. fan fixed-speed bump (55% for ~5 s) and verify RPM follows
  7. write flat-40%% + failsafe profiles to pump and fan (matches the
     stock operating point observed on this machine, plus a firmware
     ramp to 100%% by 54 C liquid)
"""

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from openkraken.backend.device import KrakenDevice
from openkraken.backend.sensors import SystemSensors
from openkraken.backend import lcd_render
from openkraken.backend.curves import software_failsafe

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        ok = False


print("[1] connect")
dev = KrakenDevice()
check("connect()", dev.connect())
check("description", "Kraken" in dev.description, dev.description)
check("firmware", bool(dev.firmware_version), dev.firmware_version)
orig_brightness = dev.lcd_brightness
orig_orientation = dev.lcd_orientation
print(f"      LCD brightness={orig_brightness}% orientation={orig_orientation} deg")

print("[2] status x2")
s1 = dev.get_status()
time.sleep(1.2)
s2 = dev.get_status()
check("connected", s1.connected and s2.connected)
check("liquid plausible", s1.liquid_temp is not None and 15 < s1.liquid_temp < 59, str(s1.liquid_temp))
check("pump rpm plausible", s1.pump_rpm is not None and 500 < s1.pump_rpm < 4000, str(s1.pump_rpm))
check("fan rpm plausible", s1.fan_rpm is not None and 0 <= s1.fan_rpm < 3000, str(s1.fan_rpm))
print(f"      liquid={s1.liquid_temp}C pump={s1.pump_rpm}rpm@{s1.pump_duty}% fan={s1.fan_rpm}rpm@{s1.fan_duty}%")

print("[3] LCD brightness round-trip")
check("set 30", dev.set_lcd_brightness(30))
time.sleep(1.0)
check("restore", dev.set_lcd_brightness(orig_brightness), f"back to {orig_brightness}%")

print("[4] live sensor frame -> LCD static upload")
sensors = SystemSensors()
sensors.read()
time.sleep(0.4)
snap = sensors.read()
data = lcd_render.LcdData(
    liquid_temp=s2.liquid_temp,
    cpu_temp=snap.cpu_temp,
    cpu_load=snap.cpu_load,
    gpu_temp=snap.gpu_temp,
    gpu_load=snap.gpu_load,
    pump_rpm=s2.pump_rpm,
    fan_rpm=s2.fan_rpm,
)
path = lcd_render.render_to_file("liquid_ring", data)
t0 = time.monotonic()
check("upload static", dev.set_lcd_static(path))
dt = time.monotonic() - t0
check("upload time < 3 s", dt < 3.0, f"{dt:.2f} s")
print("      >>> the cooler LCD should now show the rendered liquid screen <<<")
time.sleep(4.0)

print("[5] restore firmware liquid screen")
check("liquid mode", dev.set_lcd_liquid_mode())

print("[6] fan fixed-speed bump")
base_rpm = s2.fan_rpm or 0
check("fan 55%", dev.set_fixed_speed("fan", 55))
time.sleep(5.0)
s3 = dev.get_status()
print(f"      fan {base_rpm} -> {s3.fan_rpm} rpm (duty {s3.fan_duty}%)")
check("rpm rose", (s3.fan_rpm or 0) > base_rpm + 100, f"{base_rpm} -> {s3.fan_rpm}")

print("[7] settle: flat 40% + firmware failsafe on both channels")
check("fan profile", dev.set_speed_profile("fan", software_failsafe(40, "fan")))
check("pump profile", dev.set_speed_profile("pump", software_failsafe(40, "pump")))
time.sleep(5.0)
s4 = dev.get_status()
print(f"      final: liquid={s4.liquid_temp}C pump={s4.pump_rpm}rpm@{s4.pump_duty}% fan={s4.fan_rpm}rpm@{s4.fan_duty}%")
check("fan back near 40%", s4.fan_duty is not None and abs(s4.fan_duty - 40) <= 3, f"{s4.fan_duty}%")

dev.disconnect()
print("\nALL HARDWARE TESTS PASSED" if ok else "\nHARDWARE TEST FAILURES — see above")
sys.exit(0 if ok else 1)
