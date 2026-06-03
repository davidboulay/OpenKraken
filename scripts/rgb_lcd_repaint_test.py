"""Hardware test for the LCD-disturbs-ring fix (top-left LEDs revert to green).

Sequence (watch the pump ring):
  1. paint the whole ring solid purple
  2. push an LCD frame WITHOUT repaint  -> reproduces the bug (top-left green)
  3. push an LCD frame WITH repaint      -> should stay fully purple
  4. 3 rapid LCD refreshes each followed by repaint (mimics sensor animation)
Leaves the ring purple + LCD on liquid mode. Run with NO other app using the
cooler (the orchestrator stops the autostart instance first).
"""

import logging
import sys
import time

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from PIL import Image

from openkraken.backend.device import KrakenDevice

PURPLE = (124, 58, 237)
LCD_IMG = "/dev/shm/openkraken_repaint_test.png"


def banner(msg: str) -> None:
    sys.stdout.write(f"\n>>> {msg}\n")
    sys.stdout.flush()


dev = KrakenDevice()
if not dev.connect():
    sys.exit("could not connect to the cooler")

ring_leds = dev.led_count_for("ring") if hasattr(dev, "led_count_for") else 24
frame = [PURPLE] * ring_leds

# A plain dark LCD image to upload (stand-in for a rendered sensor frame).
Image.new("RGB", (640, 640), (10, 12, 18)).save(LCD_IMG)

banner(f"Painting the whole ring PURPLE ({ring_leds} LEDs). Look: all purple?")
dev.write_lighting_frame("ring", frame)
time.sleep(4)

banner("Pushing an LCD frame WITHOUT repaint -> EXPECT the bug now "
       "(a few top-left LEDs go GREEN). Watch for 5 s...")
dev.set_lcd_static(LCD_IMG)
time.sleep(5)

banner("Now repainting (the fix) -> the top-left LEDs should snap back to PURPLE.")
dev.write_lighting_frame("ring", frame)
time.sleep(4)

banner("Simulating sensor animation: 3 LCD refreshes, each followed by repaint. "
       "The ring should STAY fully purple the whole time...")
for i in range(3):
    dev.set_lcd_static(LCD_IMG)
    dev.write_lighting_frame("ring", frame)  # the engine does this via _repaint_lighting
    print(f"    refresh {i + 1}/3")
    time.sleep(2)

banner("Done. Restoring LCD to liquid mode (ring left purple; the app will "
       "re-apply your real config on restart).")
dev.set_lcd_liquid_mode()
dev.write_lighting_frame("ring", frame)
dev.disconnect()
print("\nWhat did you see? (a) green at step 2 then fixed, (b) stayed purple "
      "throughout, (c) still saw green during the 3 refreshes")
