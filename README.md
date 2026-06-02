# Kraken CAM

A Linux clone of **NZXT CAM** for the **NZXT Kraken 2024 Elite RGB** all-in-one
liquid cooler, built with **PyQt6** on top of [**liquidctl**](https://github.com/liquidctl/liquidctl).

NZXT does not ship CAM for Linux. Kraken CAM gives you a native desktop app to
monitor your loop, drive pump/fan curves that run in the cooler's own firmware,
and push live sensor screens, images, and GIFs to the round LCD — without
rebooting into Windows.

> Status: early but functional (`0.1.0`). Targets the Kraken 2024 Elite RGB
> (USB `1e71:3012`) but works with any `KrakenZ3`-class cooler liquidctl supports.

## Screenshot

```
┌──────────────────────────────────────────────────────────────┐
│  KRAKEN CAM   │   [ CPU ]  [ GPU ]  [ Liquid ]  [ Pump ]  [Fan]│
│   Dashboard   │   ╭───╮    ╭───╮    ╭───╮    ╭───╮    ╭───╮     │
│ > Cooling     │   │ 52│    │ 61│    │ 38│    │1860│   │ 983│    │
│   LCD         │   ╰───╯    ╰───╯    ╰───╯    ╰───╯    ╰───╯     │
│   Settings    │   ── temperatures ──────  ── speeds ─────────  │
│               │   /\__/‾\___       ____/‾‾‾‾‾\___              │
│ ● Kraken Elite│                                                │
└──────────────────────────────────────────────────────────────┘
```
*(placeholder — drop a real screenshot here)*

## Features

- **Live dashboard** — circular gauges for CPU/GPU/liquid temperature, pump and
  fan RPM, plus scrolling time-series graphs (selectable metrics, 1m/5m/10m
  windows).
- **Cooling control** — Silent / Balanced / Performance presets, fully editable
  pump and fan **curves** (drag-to-edit), or a flat fixed duty. Curves can track
  **liquid**, **CPU**, or **GPU** temperature.
  - **Liquid-temp curves run inside the cooler's firmware** and keep working
    even after Kraken CAM is closed.
  - **CPU/GPU-temp curves** are driven by the app each tick, with a firmware
    failsafe baked in so a crashed app can never cook the loop.
- **LCD screen control** for the 640×640 round display:
  - Firmware liquid-temp screen
  - Live **sensor screens** rendered on the host and uploaded over USB
    (multiple styles)
  - **Static images** and **animated GIFs**
  - Brightness, orientation (0/90/180/270°), and a software "off"
- **System tray** with quick profile switching and close-to-tray.
- **Persistent config** at `~/.config/kraken-cam/config.json`.

## Requirements

- Linux (developed on **Pop!_OS / COSMIC / Wayland**; works on GNOME and others).
- **Python ≥ 3.10** (3.12 recommended).
- **PyQt6 ≥ 6.4** — best installed from your distribution (e.g.
  `sudo apt install python3-pyqt6` on Debian/Ubuntu/Pop!_OS). `setup.sh` will
  fall back to installing PyQt6 from PyPI into the venv if no system package is
  found.
- **liquidctl ≥ 1.15** with support for your device. The Kraken 2024 Elite RGB
  (`1e71:3012`) needs a build that knows that USB id; `setup.sh` checks and, if
  necessary, upgrades liquidctl from upstream automatically.
- **Pillow ≥ 9** (for LCD rendering) — installed automatically.
- A C-less install: there are **no compiled extensions** in this project itself.

## Install

```sh
git clone <this-repo> kraken-cam
cd kraken-cam
./setup.sh
```

`setup.sh` is idempotent and:

1. Creates `.venv/` with `--system-site-packages` (so the system PyQt6 is
   visible).
2. Installs the project (`pip install -e .`) and its dependencies.
3. Verifies the liquidctl driver supports your Kraken (upgrades from the
   upstream git repo if `0x3012` is missing).
4. Verifies PyQt6 is importable (installs it into the venv if not).
5. Installs a `kraken-cam.desktop` launcher into
   `~/.local/share/applications/` with absolute `Exec`/`Icon` paths.

## Run

From a terminal:

```sh
.venv/bin/kraken-cam
```

Or launch **Kraken CAM** from your application menu.

Flags:

| Flag             | Effect                                  |
| ---------------- | --------------------------------------- |
| `--minimized`    | Start hidden in the system tray         |
| `--config PATH`  | Use an alternate config file            |
| `--debug`        | Verbose (DEBUG-level) logging           |
| `--version`      | Print version and exit                  |

### Autostart on login

Because curves can run autonomously in the cooler's firmware you often don't
need the app running, but if you want monitoring or CPU/GPU-temp curves at
login:

- **COSMIC / GNOME / most desktops:** drop a copy of the launcher into the
  autostart directory:

  ```sh
  mkdir -p ~/.config/autostart
  cp ~/.local/share/applications/kraken-cam.desktop ~/.config/autostart/
  # optionally start hidden in the tray:
  sed -i 's|kraken-cam$|kraken-cam --minimized|' ~/.config/autostart/kraken-cam.desktop
  ```

## Permissions

liquidctl talks to the cooler over raw USB HID (`/dev/hidraw*` / the USB device
node). To use it **without root**, your user needs write access to the device.
On this machine that already works; on a fresh system add a udev rule for NZXT's
vendor id `1e71`:

Create `/etc/udev/rules.d/99-kraken-cam.rules`:

```udev
# NZXT devices (incl. Kraken 2024 Elite RGB, 1e71:3012) — accessible to plugdev
SUBSYSTEMS=="usb", ATTRS{idVendor}=="1e71", MODE="0660", TAG+="uaccess", GROUP="plugdev"
KERNEL=="hidraw*", ATTRS{idVendor}=="1e71", MODE="0660", TAG+="uaccess", GROUP="plugdev"
```

Then reload and re-plug (or reboot):

```sh
sudo usermod -aG plugdev "$USER"      # if you rely on the plugdev group
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The `TAG+="uaccess"` line is usually enough on systemd-logind desktops; the
`plugdev` group is a fallback for systems without logind seat management.

## Supported devices

Any `KrakenZ3`-class cooler that liquidctl recognises. LCD size and speed
channels are detected automatically from the driver:

| Model                         | USB id        | LCD       |
| ----------------------------- | ------------- | --------- |
| Kraken Z53 / Z63 / Z73        | `1e71:3008`   | 320×320   |
| Kraken 2023                   | `1e71:300e`   | 240×240   |
| Kraken 2023 Elite             | `1e71:300c`   | 640×640   |
| **Kraken 2024 Elite RGB**     | `1e71:3012`   | 640×640   |
| Kraken 2024 Plus              | `1e71:3014`   | 240×240   |

The non-LCD Kraken X-series and other NZXT coolers are out of scope (no screen).

## FAQ

**Does it control the RGB lighting?**
No. liquidctl currently exposes **no lighting protocol** for the 2023/2024
Kraken generation (the driver's colour-channel map is empty), so Kraken CAM
deliberately does not implement RGB. This will be revisited if/when upstream
liquidctl adds support.

**Why is the LCD sensor screen refresh so slow by default?**
Each rendered frame is a full-resolution bitmap uploaded over USB — roughly
**0.8 MB per frame** (640×640 RGB565, ≈819 KB). To avoid saturating the link and
spamming the firmware, the **sensor screen defaults to a 2-second** refresh
interval (configurable 0.5–10 s on the LCD page). Animated GIFs are limited by
the firmware to an encoded size of about **24 MB**.

**Do my fan/pump curves survive closing the app — or a reboot?**
**Liquid-temp curves** are written into the cooler's firmware as a 40-point
table and **keep running after you close Kraken CAM** (and across an OS reboot,
as long as the cooler stays powered). They are **reset when the cooler loses
power** (e.g. a full AC power cycle), after which you should reopen the app (or
let it autostart with "apply on start") to re-apply them. **CPU/GPU-temp
curves** require the app to be running because the host computes the duty each
tick; a firmware failsafe still protects the loop if the app dies.

**Why isn't there an "LCD off" button that actually turns the panel off?**
The firmware has no blank mode, so "Screen off" sets brightness to 0 and
restores your previous brightness when you switch back.

**It says "Disconnected".**
Check the [permissions](#permissions) section, confirm `liquidctl list` (run via
the venv: `.venv/bin/python -m liquidctl list`) sees the device, and make sure
nothing else (e.g. another monitoring tool) is holding the HID handle.

## Uninstall

```sh
rm ~/.local/share/applications/kraken-cam.desktop
rm ~/.config/autostart/kraken-cam.desktop   # if you added autostart
rm -rf .venv                                 # the project's virtual environment
rm -rf ~/.config/kraken-cam                  # config + cached LCD media (optional)
# then delete the cloned project directory
```

Any udev rule you added (`/etc/udev/rules.d/99-kraken-cam.rules`) can be removed
separately with `sudo`.

## License

MIT — see [`LICENSE`](LICENSE). Each source file carries an
`SPDX-License-Identifier: MIT` header.

Kraken CAM is an independent project and is **not affiliated with or endorsed by
NZXT**. "NZXT", "Kraken", and "CAM" are trademarks of their respective owners.
Built on the excellent [liquidctl](https://github.com/liquidctl/liquidctl).
