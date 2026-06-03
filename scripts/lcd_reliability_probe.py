"""Measure LCD bucket-switch reliability while Wine plugins hold the HID device.

Runs N cycles of (upload static image -> switch to liquid mode), counting how
many fail with the 'Failed to switch active bucket' / missing-message symptom.
LCD-only, restores liquid mode at the end. Run with the OpenKraken app stopped.
"""

import logging
import sys
import time

# Capture driver-level errors (the bucket-switch failure logs at ERROR there).
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

from PIL import Image

from openkraken.backend.device import KrakenDevice

N = 30
IMG = "/dev/shm/openkraken_reliability.png"
Image.new("RGB", (640, 640), (10, 12, 18)).save(IMG)

dev = KrakenDevice()
if not dev.connect():
    sys.exit("could not connect")

static_fail = 0
liquid_fail = 0
for i in range(N):
    if not dev.set_lcd_static(IMG):
        static_fail += 1
    if not dev.set_lcd_liquid_mode():
        liquid_fail += 1
    time.sleep(0.4)
    sys.stdout.write(f"\r  cycle {i + 1}/{N}  static_fail={static_fail} liquid_fail={liquid_fail}")
    sys.stdout.flush()

dev.set_lcd_liquid_mode()
dev.disconnect()
total = static_fail + liquid_fail
print(f"\n\nRESULT: {total} failures across {N*2} LCD ops "
      f"({static_fail} static, {liquid_fail} liquid).")
print("=> Wine plugins IMPACT OpenKraken" if total > 1
      else "=> Wine plugins do NOT meaningfully impact OpenKraken")
