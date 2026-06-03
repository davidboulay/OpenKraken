"""Watch test for the double-buffered sensor uploader.

Streams 20 visibly-changing sensor frames at 2s intervals using
KrakenDevice.set_lcd_sensor_frame (double-buffered). WATCH THE COOLER: the
sensor screen should update smoothly with NO flash to black or to the firmware
liquid screen between frames. Run with the app stopped.
"""

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from openkraken.backend.device import KrakenDevice
from openkraken.backend import lcd_render

dev = KrakenDevice()
if not dev.connect():
    sys.exit("could not connect")

print(">>> Streaming 20 frames @2s via double-buffer. WATCH for black/liquid flashes.")
ok = 0
held = 0
data = lcd_render.LcdData(liquid_temp=35.0, cpu_temp=55.0, cpu_load=0.0,
                          gpu_temp=49.0, gpu_load=5.0, pump_rpm=2100, fan_rpm=900)
for i in range(20):
    data.cpu_load = float((i * 5) % 100)      # changes each frame so updates are visible
    data.liquid_temp = 30.0 + (i % 10)
    path = lcd_render.render_to_file("liquid_ring", data)
    if dev.set_lcd_sensor_frame(path):
        ok += 1
    else:
        held += 1
    print(f"  frame {i+1}/20: {'shown' if ok else 'held'}  (active_buffer={dev._lcd_active_buffer})")
    time.sleep(2)

print(f"\nRESULT: {ok} frames shown, {held} held (previous frame kept).")
print(">>> Did the screen update SMOOTHLY with no black/liquid flash? (your eyes)")
print(">>> Leaving on the last sensor frame; the app will resume on restart.")
dev.disconnect()
