# OpenKraken

*A native Linux desktop app for NZXT Kraken liquid coolers — monitor, tune curves, and drive the LCD without rebooting into Windows.*

A Linux clone of **NZXT CAM** for the **NZXT Kraken 2024 Elite RGB** all-in-one
liquid cooler, built with **PyQt6** on top of [**liquidctl**](https://github.com/liquidctl/liquidctl).

NZXT does not ship CAM for Linux. OpenKraken gives you a native desktop app to
monitor your loop, drive pump/fan curves that run in the cooler's own firmware,
and push live sensor screens, images, and GIFs to the round LCD — without
rebooting into Windows.

> Status: early but functional (`0.1.0`). Targets the Kraken 2024 Elite RGB
> (USB `1e71:3012`) but works with any `KrakenZ3`-class cooler liquidctl supports.

## Screenshot

```
┌──────────────────────────────────────────────────────────────┐
│  OPENKRAKEN   │   [ CPU ]  [ GPU ]  [ Liquid ]  [ Pump ]  [Fan]│
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
    even after OpenKraken is closed.
  - **CPU/GPU-temp curves** are driven by the app each tick, with a firmware
    failsafe baked in so a crashed app can never cook the loop.
- **LCD screen control** for the 640×640 round display:
  - Firmware liquid-temp screen
  - Live **sensor screens** rendered on the host and uploaded over USB
    (multiple styles)
  - **Static images** and **animated GIFs**
  - Brightness, orientation (0/90/180/270°), and a software "off"
- **RGB lighting** (off by default) for the 24-LED pump ring and the bundled
  RGB Core fan chain: solid colours plus host-streamed Breathing / Color cycle /
  Spectrum effects, with per-channel brightness. The wire protocol was
  reverse-engineered by the community (liquidctl PR #882 / OpenRGB) since NZXT
  ships no public spec; effects stream from the app at the device's ~1 frame/s
  ceiling. See the [FAQ](#faq) for the caveats.
- **System tray** with quick profile switching and close-to-tray.
- **Persistent config** at `~/.config/openkraken/config.json`.

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
git clone https://github.com/davidboulay/OpenKraken openkraken
cd openkraken
./setup.sh
```

`setup.sh` is idempotent and:

1. Creates `.venv/` with `--system-site-packages` (so the system PyQt6 is
   visible).
2. Installs the project (`pip install -e .`) and its dependencies.
3. Verifies the liquidctl driver supports your Kraken (upgrades from the
   upstream git repo if `0x3012` is missing).
4. Verifies PyQt6 is importable (installs it into the venv if not).
5. Installs an `openkraken.desktop` launcher into
   `~/.local/share/applications/` with absolute `Exec`/`Icon` paths.

## Run

From a terminal:

```sh
.venv/bin/openkraken
```

Or launch **OpenKraken** from your application menu.

Flags:

| Flag             | Effect                                  |
| ---------------- | --------------------------------------- |
| `--minimized`    | Start hidden (in the tray, or in the background without one) |
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
  cp ~/.local/share/applications/openkraken.desktop ~/.config/autostart/
  # optionally start hidden in the tray:
  sed -i 's|openkraken$|openkraken --minimized|' ~/.config/autostart/openkraken.desktop
  ```

## GNOME / other desktops

OpenKraken works on stock GNOME, KDE, COSMIC, and others. The one thing that
differs between desktops is the **system tray**:

- **With a tray** (KDE, COSMIC's panel applet, GNOME *with* the
  [AppIndicator/KStatusNotifierItem extension](https://extensions.gnome.org/extension/615/appindicator-support/)),
  you get the tray icon, quick profile switching, and close-to-tray.
- **Without a tray** (stock GNOME ships none by default), the app still keeps
  your cooling and lighting active when you close the window:
  - **Closing the window hides it** and leaves the control engine running in the
    background (so CPU/GPU-temp curves and sensor LCD screens keep updating).
    This is the **"Keep running in background when closed"** option on the
    Settings page (enabled by default; ignored when a tray is present, where
    *close to tray* applies instead).
  - **Relaunch OpenKraken** — from the application menu, the launcher, or
    `.venv/bin/openkraken` — to **reopen the window**. A second instance does not
    start; it just raises the running one (single-instance via a per-user socket
    at `openkraken-$UID`).
  - **`Ctrl+Q` quits** the app entirely (stops the engine and exits). Without a
    tray this is the discoverable way to fully quit; you can also kill the
    process.

If you only ever rely on **liquid-temp curves** (which run inside the cooler's
firmware), you do not need the app running at all after applying them once.

### Wayland note

PyQt6 at this project's floor (**≥ 6.4**, the version `setup.sh`/`pip` install
and the one shipped on the target system) bundles the Qt **Wayland** platform
plugin, so the app runs natively on Wayland sessions. If the Wayland plugin is
missing, Qt falls back to **XWayland**, which is harmless — the only visible
difference is that window raising on relaunch is a *request* the compositor may
honour or queue, per Wayland's focus-stealing policy.

## Permissions

liquidctl talks to the cooler over raw USB HID (`/dev/hidraw*` / the USB device
node). To use it **without root**, your user needs write access to the device.
On this machine that already works. **`setup.sh` checks this for you**: it probes
for an NZXT (`1e71`) `hidraw` node and, if one is present but not read/writable by
your user, *offers* to install the udev rule below (it never fails setup if you
decline, and stays silent when run non-interactively).

To add the rule **manually**, create `/etc/udev/rules.d/70-openkraken.rules`:

```udev
# NZXT devices (incl. Kraken 2024 Elite RGB, 1e71:3012) — accessible to plugdev
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1e71", TAG+="uaccess", MODE="0660", GROUP="plugdev"
```

Then reload and re-plug (or reboot):

```sh
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev "$USER"      # only if you rely on the plugdev fallback
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
Yes — but it's **off by default**, and you should know how it works first.
Installed liquidctl exposes **no lighting protocol** for the 2023/2024 Kraken
generation (the driver's colour-channel map is empty), so OpenKraken speaks the
HUE2 "Direct" wire protocol itself. That protocol was **reverse-engineered by
the community** — credit to the unmerged [liquidctl PR #882](https://github.com/liquidctl/liquidctl/pull/882)
(`feat/kraken-2024-elite-rgb`, tested on this exact device) and to the
[OpenRGB](https://openrgb.org/) NZXT HUE2 controller and its device issues
(#4828 / #4985). There is no official NZXT spec.

What that means in practice:

- **Enable it explicitly.** The Lighting page ships with "Control LEDs"
  unchecked; until you turn it on, OpenKraken never writes to the LEDs and your
  existing (NZXT/firmware) lighting is left alone.
- **Effects are streamed from the host at ~1 frame/s.** The device's firmware
  rejects its own hardware animation modes for this generation and reliably
  accepts Direct frames only at about **1 FPS**, so Breathing / Color cycle /
  Spectrum are computed by the app and pushed one slow frame at a time. They are
  designed as gentle drifts, not fast flashes, and they **stop when the app
  closes** (a solid colour just stays as the last frame written).
- **Brightness is applied host-side** (the device has no brightness command),
  and **colours reset on an AC power-cycle** — reopen the app (or let it
  autostart with "apply on start") to re-apply.
- Covers the **24-LED pump ring** and the bundled **RGB Core fan** chain; ring
  and fans can be synced or driven independently.

**Why is the LCD sensor screen refresh so slow by default?**
Each rendered frame is a full-resolution bitmap uploaded over USB — roughly
**0.8 MB per frame** (640×640 RGB565, ≈819 KB). To avoid saturating the link and
spamming the firmware, the **sensor screen defaults to a 2-second** refresh
interval (configurable 0.5–10 s on the LCD page). Animated GIFs are limited by
the firmware to an encoded size of about **24 MB**.

**Do my fan/pump curves survive closing the app — or a reboot?**
**Liquid-temp curves** are written into the cooler's firmware as a 40-point
table and **keep running after you close OpenKraken** (and across an OS reboot,
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
rm ~/.local/share/applications/openkraken.desktop
rm ~/.config/autostart/openkraken.desktop   # if you added autostart
rm -rf .venv                                 # the project's virtual environment
rm -rf ~/.config/openkraken                  # config + cached LCD media (optional)
# then delete the cloned project directory
```

Any udev rule that was installed (`/etc/udev/rules.d/70-openkraken.rules`) can be
removed separately with `sudo`.

## License

MIT — see [`LICENSE`](LICENSE). Each source file carries an
`SPDX-License-Identifier: MIT` header.

OpenKraken is an independent open-source project, not affiliated with or endorsed
by NZXT. "Kraken" is referenced descriptively for hardware interoperability.
"NZXT", "Kraken", and "CAM" are trademarks of their respective owners.
Built on the excellent [liquidctl](https://github.com/liquidctl/liquidctl).
