"""Verify LCD sensor uploads self-heal across bucket exhaustion (40 frames).

With the detect-clear-retry fix in device._set_screen, set_lcd_static should
return True for every frame even past the ~30-frame point where the device's
bucket memory exhausts. Reports recoveries (cleared+retried) vs hard failures.
Run with the app stopped.
"""

import logging
import sys
import time

from openkraken.backend.device import KrakenDevice
from openkraken.backend import lcd_render

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
# Count how often the wrapper had to clear+retry.
recoveries = {"n": 0}
_orig = logging.getLogger("openkraken.backend.device").info


class _CountRecover(logging.Handler):
    def emit(self, record):
        if "clearing buckets and retrying" in record.getMessage():
            recoveries["n"] += 1


logging.getLogger("openkraken.backend.device").addHandler(_CountRecover())
logging.getLogger("openkraken.backend.device").setLevel(logging.INFO)

dev = KrakenDevice()
if not dev.connect():
    sys.exit("could not connect")

data = lcd_render.LcdData(liquid_temp=35.0, cpu_temp=55.0, cpu_load=20.0,
                          gpu_temp=49.0, gpu_load=5.0, pump_rpm=2100, fan_rpm=900)

hard_fail = 0
N = 40
for i in range(N):
    # vary a value so each frame differs (forces a real re-render/upload)
    data.cpu_load = float(i % 100)
    path = lcd_render.render_to_file("liquid_ring", data)
    if not dev.set_lcd_static(path):
        hard_fail += 1
    sys.stdout.write(f"\r  frame {i+1}/{N}  recoveries={recoveries['n']}  hard_fail={hard_fail}")
    sys.stdout.flush()
    time.sleep(1.0)

print(f"\n\nRESULT over {N} frames: {recoveries['n']} self-heal recoveries, "
      f"{hard_fail} hard failures.")
print("PASS: sensor mode survives bucket exhaustion" if hard_fail == 0
      else "FAIL: frames still going dark after retry")
dev.set_lcd_liquid_mode()
dev.disconnect()
