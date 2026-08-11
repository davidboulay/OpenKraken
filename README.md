<div align="center">

<img src="openkraken/resources/openkraken.svg" alt="" width="104" height="104">

<h1>OpenKraken</h1>

**NZXT Kraken control for Linux** — monitor your loop, tune firmware fan curves,<br>
and drive the round LCD, without rebooting into Windows.

[![Release](https://img.shields.io/github/v/release/davidboulay/OpenKraken?style=flat-square&color=7C3AED&labelColor=1c1c1e)](https://github.com/davidboulay/OpenKraken/releases/latest)
[![APT repo](https://img.shields.io/badge/apt-davidboulay.github.io%2FOpenKraken-7C3AED?style=flat-square&labelColor=1c1c1e)](https://davidboulay.github.io/OpenKraken/)
[![Platform](https://img.shields.io/badge/Linux-FC6F8C?style=flat-square&labelColor=1c1c1e&label=runs%20on)](#get-openkraken)
[![License](https://img.shields.io/github/license/davidboulay/OpenKraken?style=flat-square&color=6b7280&labelColor=1c1c1e)](LICENSE)

[**Install**](#get-openkraken) · [Features](#features) · [Updating](#updating) · [Permissions](#permissions) · [Supported devices](#supported-devices) · [FAQ](#faq)

</div>

<br>

NZXT does not ship CAM for Linux. OpenKraken is a native **PyQt6** desktop app on
top of [**liquidctl**](https://github.com/liquidctl/liquidctl): live gauges and
graphs for your loop, pump/fan **curves that run inside the cooler's own
firmware**, live **sensor screens / images / GIFs** on the round LCD, and RGB
lighting over a community-reverse-engineered protocol.

Developed on **Pop!_OS / COSMIC / Wayland** against a **Kraken 2024 Elite RGB**;
works with any `KrakenZ3`-class cooler liquidctl recognises, on GNOME, KDE, and
other desktops.

![OpenKraken sensor screen on an NZXT Kraken 2024 Elite](docs/images/cooler.png)

<sub>**The cooler itself** — a live sensor screen rendered by OpenKraken on the 640×640 LCD: liquid ring, CPU/GPU temps with vendor badges.</sub>

| Dashboard | Cooling |
| --- | --- |
| ![Dashboard](docs/images/dashboard.png) | ![Cooling curves](docs/images/cooling.png) |
| **LCD** | **Lighting** |
| ![LCD control](docs/images/lcd.png) | ![RGB lighting](docs/images/lighting.png) |

<sub>The AMD / NVIDIA marks on the LCD sensor screen are user-supplied logo files
(see [Custom vendor logos](#features)); OpenKraken ships none.</sub>

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
- **LCD screen control** for the round display:
  - Firmware liquid-temp screen
  - Live **sensor screens** rendered on the host and uploaded over USB
    (multiple styles), with a configurable liquid-ring colour and
    auto-detected CPU/GPU **vendor badges** (AMD / Intel / NVIDIA)
  - **Static images** and **animated GIFs**
  - Brightness, orientation (0/90/180/270°), and a software "off"
  - Self-healing: a wedged panel (a firmware quirk this device has) is detected
    and recovered automatically — no more black screens
- **RGB lighting** (off by default) for the 24-LED pump ring and the bundled
  RGB Core fan chain: solid colours plus smooth host-streamed Breathing /
  Color cycle / Spectrum effects, with per-channel brightness. See the
  [FAQ](#faq) for how this works and its caveats.
- **In-app updates** — checks GitHub on launch (optional) and prompts with a
  desktop notification when a new version is out; one click updates and
  restarts. See [Updating](#updating).
- **System tray** with quick profile switching and close-to-tray; runs in the
  background on desktops without a tray.
- **Persistent config** at `~/.config/openkraken/config.json`.

*Custom vendor logos:* the sensor screens show a stylised vendor wordmark by
default. To use official logo artwork instead, drop your own RGBA PNGs at
`~/.config/openkraken/logos/{amd,intel,nvidia}.png` — OpenKraken ships no
trademarked logos.

## Get OpenKraken

### Ubuntu / Pop!_OS / Debian — APT repository (recommended)

Add the repo once, then install and get updates with `apt` like any system
package:

```bash
curl -fsSL https://davidboulay.github.io/OpenKraken/openkraken.gpg | sudo tee /usr/share/keyrings/openkraken.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/openkraken.gpg] https://davidboulay.github.io/OpenKraken ./" | sudo tee /etc/apt/sources.list.d/openkraken.list
sudo apt update && sudo apt install openkraken
```

New versions then arrive with `sudo apt upgrade`. The repo is GPG-signed and
served over GitHub Pages. The `.deb` installs the udev rule for you — plug the
cooler (or re-plug it) and launch **OpenKraken** from your app list.

### Or grab the `.deb` directly

From the **[Releases page](https://github.com/davidboulay/OpenKraken/releases/latest)**:

```bash
gh release download --repo davidboulay/OpenKraken --pattern '*.deb'
sudo apt install ./openkraken_*.deb
```

### From source

```sh
git clone https://github.com/davidboulay/OpenKraken openkraken
cd openkraken
./setup.sh
```

`setup.sh` is idempotent: it creates `.venv/` (with `--system-site-packages` so
a distro PyQt6 is visible), installs the project editable, verifies the
liquidctl driver knows your Kraken (upgrading from upstream if needed), checks
device permissions (offering the udev rule if yours are missing — see
[Permissions](#permissions)), and installs an `openkraken.desktop` launcher.

Requirements are modest: Python ≥ 3.10, PyQt6 ≥ 6.4, liquidctl ≥ 1.15,
Pillow ≥ 9 — no compiled extensions in the project itself.

### Run

Launch **OpenKraken** from your application menu, or from a terminal:

```sh
.venv/bin/openkraken        # source install; the .deb puts `openkraken` on PATH
```

| Flag             | Effect                                  |
| ---------------- | --------------------------------------- |
| `--minimized`    | Start hidden (in the tray, or in the background without one) |
| `--config PATH`  | Use an alternate config file            |
| `--debug`        | Verbose (DEBUG-level) logging           |
| `--version`      | Print version and exit                  |

**Autostart on login** (for monitoring or CPU/GPU-temp curves):

```sh
mkdir -p ~/.config/autostart
cp ~/.local/share/applications/openkraken.desktop ~/.config/autostart/
# optionally start hidden in the tray:
sed -i 's|openkraken$|openkraken --minimized|' ~/.config/autostart/openkraken.desktop
```

Liquid-temp curves run inside the cooler's firmware, so if that's all you use,
the app doesn't need to be running after applying them once.

## Updating

- **APT installs** update like any package: `sudo apt upgrade`.
- **In-app**: with *Check for updates on launch* enabled (Settings, default on),
  OpenKraken quietly checks GitHub at startup and shows a desktop notification
  when a new version exists — **Update now** downloads and installs it (a
  PolicyKit password prompt appears for `.deb` installs; source checkouts
  `git pull`) and restarts the app. The same flow is available manually via
  **Settings → Check for updates**.

## Permissions

liquidctl talks to the cooler over raw USB HID (`/dev/hidraw*`) **and** the raw
USB device node (string descriptors at enumeration + the LCD's bulk interface).
To use it **without root**, your user needs access to both. The `.deb` installs
the udev rule automatically; `setup.sh` probes both access paths and *offers*
the rule when either is missing.

To add it **manually**, create `/etc/udev/rules.d/70-openkraken.rules`:

```udev
# NZXT devices (incl. Kraken 2024 Elite RGB, 1e71:3012) — accessible to plugdev.
# BOTH lines are required: "hidraw" covers cooling/lighting/status, and "usb"
# covers the LCD (a separate USB bulk interface) plus device enumeration itself
# — without it OpenKraken fails to connect with a "no langid (permission
# issue...)" error.
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1e71", TAG+="uaccess", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1e71", TAG+="uaccess", MODE="0660", GROUP="plugdev"
```

Then reload and re-plug (or reboot):

```sh
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev "$USER"      # only if you rely on the plugdev fallback
```

The `TAG+="uaccess"` line is usually enough on systemd-logind desktops; the
`plugdev` group is a fallback for systems without logind seat management.

## Desktops & tray

OpenKraken works on stock GNOME, KDE, COSMIC, and others. The one thing that
differs is the **system tray**:

- **With a tray** (KDE, COSMIC's panel applet, GNOME *with* the
  [AppIndicator/KStatusNotifierItem extension](https://extensions.gnome.org/extension/615/appindicator-support/)),
  you get the tray icon, quick profile switching, and close-to-tray.
- **Without a tray** (stock GNOME), closing the window hides it and leaves the
  control engine running (*"Keep running in background when closed"*, Settings,
  default on). **Relaunching OpenKraken reopens the window** (single-instance —
  a second launch just raises the running one). <kbd>Ctrl</kbd>+<kbd>Q</kbd>
  quits entirely.

PyQt6 ≥ 6.4 bundles the Qt Wayland platform plugin, so the app runs natively on
Wayland; if the plugin is missing Qt falls back to XWayland, which is harmless.

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
- **Effects are streamed from the host.** The device's firmware rejects its own
  hardware animation modes for this generation, so Breathing / Color cycle /
  Spectrum are computed by the app and streamed as Direct frames — smoothly, at
  **4 frames/s by default** (`lighting_fps` in the config, clamped 0.5–5; 5 FPS
  soak-tested on real hardware). Effects **stop when the app closes** (a solid
  colour just stays as the last frame written).
- **Brightness is applied host-side** (the device has no brightness command),
  and **colours reset on an AC power-cycle** — reopen the app (or let it
  autostart with "apply on start") to re-apply.
- Covers the **24-LED pump ring** and the bundled **RGB Core fan** chain; ring
  and fans can be synced or driven independently.

**Why is the LCD sensor screen refresh 2 s by default?**
Each rendered frame is a full-resolution bitmap uploaded over USB — roughly
**0.8 MB per frame**. To avoid saturating the link and spamming the firmware,
the **sensor screen defaults to a 2-second** refresh interval (configurable
0.5–10 s on the LCD page). Animated GIFs are limited by the firmware to an
encoded size of about **24 MB**.

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

## Configuration & data

Everything lives under `~/.config/openkraken/`:

- `config.json` — all settings (cooling curves, LCD, lighting, update checks,
  `lighting_fps`, LCD self-heal intervals)
- `media/` — cached/resized LCD images and GIFs
- `logos/` — optional user-supplied vendor logo PNGs for the sensor screens

## Uninstall

- **APT / .deb:** `sudo apt remove openkraken` (add `--purge` to drop the udev
  rule too).
- **Source install:**

  ```sh
  rm ~/.local/share/applications/openkraken.desktop
  rm ~/.config/autostart/openkraken.desktop   # if you added autostart
  rm -rf .venv                                 # the project's virtual environment
  rm -rf ~/.config/openkraken                  # config + cached LCD media (optional)
  # then delete the cloned project directory
  ```

  Any udev rule that was installed (`/etc/udev/rules.d/70-openkraken.rules`) can
  be removed separately with `sudo`.

## License

MIT — see [`LICENSE`](LICENSE). Each source file carries an
`SPDX-License-Identifier: MIT` header.

OpenKraken is an independent open-source project, not affiliated with or endorsed
by NZXT. "Kraken" is referenced descriptively for hardware interoperability.
"NZXT", "Kraken", and "CAM" are trademarks of their respective owners.
Built on the excellent [liquidctl](https://github.com/liquidctl/liquidctl).
