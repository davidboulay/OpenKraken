# Packaging OpenKraken

This directory holds three ways to install [OpenKraken](https://github.com/davidboulay/OpenKraken):

1. A **Debian package** (`openkraken_<version>_amd64.deb`) for Debian, Ubuntu,
   Pop!\_OS, Linux Mint, and other apt-based distributions.
2. An **Arch package** (`openkraken-<version>-1-any.pkg.tar.zst`) for Arch,
   Omarchy, EndeavourOS, Manjaro, and other pacman-based distributions.
3. A **universal installer** (`install.sh`) that works on any Linux distribution
   by cloning the source and running the project's own `setup.sh`.

| File                     | What it does                                                   |
| ------------------------ | -------------------------------------------------------------- |
| `build-deb.sh`           | Builds the `.deb` into `dist/` using `dpkg-deb`.                |
| `arch/PKGBUILD`          | Arch package recipe; packages the surrounding checkout.          |
| `arch/build-pkg.sh`      | Wrapper: syncs `pkgver`, runs `makepkg`, optionally installs.    |
| `arch/openkraken.install`| pacman scriptlet: re-applies the udev rule to a live cooler.     |
| `arch/70-openkraken.rules`| The Arch udev rule (`uaccess`, no `plugdev`).                   |
| `omarchy/openkraken.lua` | Optional Hyprland window rules + autostart for Omarchy.         |
| `install.sh`             | Curl-able installer: clones the repo and runs `setup.sh`.        |

---

## Quick install (any distro)

The fastest path — no build, no clone by hand:

```sh
curl -fsSL https://raw.githubusercontent.com/davidboulay/OpenKraken/main/packaging/install.sh | bash
```

This:

1. Checks for `git` and `python3` (≥ 3.10), with per-distro hints if anything is
   missing (`apt` / `dnf` / `pacman`).
2. Clones the source into `~/.local/share/openkraken-src` (or `git pull`s an
   existing checkout there).
3. Runs the project's idempotent `setup.sh` (creates a venv, installs the app
   and dependencies, upgrades liquidctl if needed, installs a desktop launcher).

It never needs root. `setup.sh` only *offers* to install a udev rule, and only
when run interactively in a terminal — piping it through `bash` never prompts.

To install into a different directory:

```sh
OPENKRAKEN_SRC_DIR=/opt/openkraken-src bash install.sh
```

---

## Building the Debian package

From this directory (or anywhere — the script locates the project root itself):

```sh
./build-deb.sh
```

The build needs network access (it runs `pip install --target` to vendor
liquidctl). It produces:

```
packaging/dist/openkraken_<version>_amd64.deb
```

### What goes in the package

| Path                                               | Contents                                  |
| -------------------------------------------------- | ----------------------------------------- |
| `/usr/lib/openkraken/openkraken/`                  | The Python package, copied from source.   |
| `/usr/lib/openkraken/vendor/`                      | `liquidctl >= 1.15` + its dependencies.   |
| `/usr/bin/openkraken`                              | Launcher (adds both dirs to `sys.path`).  |
| `/usr/share/applications/openkraken.desktop`       | Application-menu entry.                   |
| `/usr/share/icons/hicolor/scalable/apps/openkraken.svg` | App icon.                            |
| `/usr/lib/udev/rules.d/70-openkraken.rules`        | Non-root NZXT device access.              |
| `/usr/share/doc/openkraken/`                       | `README.md`, `PROTOCOL.md`, `copyright`.  |

**PyQt6 and Pillow are not bundled** — they come from the distribution via the
package `Depends:` (`python3-pyqt6`, `python3-pil`). Only **liquidctl** is
vendored, because Debian/Ubuntu stable releases often ship a version too old to
know the Kraken 2024 Elite RGB (`1e71:3012`).

The package declares:

```
Depends: python3 (>= 3.10), python3-pyqt6, python3-pil, fonts-dejavu-core
```

`fonts-dejavu-core` is there for the same reason the Arch package depends on
`ttf-font`: the LCD sensor screens need a real TrueType face, and PIL's built-in
bitmap font is unreadable at the 210 px temperature readout.

The maintainer scripts reload udev rules (`udevadm control --reload-rules &&
udevadm trigger`) and refresh the desktop database (`update-desktop-database`)
on install and removal, so device access and the launcher work without a reboot.

### Inspecting the built package

```sh
dpkg-deb -I packaging/dist/openkraken_*_amd64.deb   # control metadata
dpkg-deb -c packaging/dist/openkraken_*_amd64.deb   # file listing
lintian   packaging/dist/openkraken_*_amd64.deb     # optional lint (if installed)
```

### Installing the `.deb`

```sh
sudo apt install ./packaging/dist/openkraken_<version>_amd64.deb
```

Using `apt install ./file.deb` (rather than `dpkg -i`) pulls in the
`python3-pyqt6` / `python3-pil` dependencies automatically. On a plain `dpkg -i`
you may need `sudo apt-get -f install` afterwards to satisfy them.

After install, launch **OpenKraken** from the application menu, or run
`openkraken` from a terminal. Useful flags:

| Flag           | Effect                                              |
| -------------- | --------------------------------------------------- |
| `--minimized`  | Start hidden (tray if available, else background).  |
| `--config PATH`| Use an alternate config file.                       |
| `--debug`      | Verbose (DEBUG-level) logging.                       |
| `--version`    | Print version and exit.                             |

### Removing the package

```sh
sudo apt remove openkraken      # or: sudo dpkg -r openkraken
```

Your configuration in `~/.config/openkraken/` is left untouched.

---

## Building the Arch package

```sh
cd arch
./build-pkg.sh --install     # build, then install with pacman
./build-pkg.sh               # build only, into arch/dist/
makepkg -si                  # equivalent; the PKGBUILD is self-sufficient
```

Unlike the `.deb` build this needs **no network**: nothing is vendored, because
Arch's own repositories carry everything, including liquidctl ≥ 1.16 with Kraken
2024 support. The result is ~150 KB rather than ~24 MB.

The `PKGBUILD` packages the checkout it lives in (via `$startdir/../..`), so
`makepkg` here builds your working tree — the same ergonomics as `build-deb.sh`
and `setup.sh`. `prepare()` refuses to build if `pkgver` has drifted from
`pyproject.toml`, so a version bump can never ship a mislabelled package;
`build-pkg.sh` syncs it for you (and resets `pkgrel` to 1).

### What goes in the package

| Path                                               | Contents                                  |
| -------------------------------------------------- | ----------------------------------------- |
| `/usr/lib/openkraken/openkraken/`                  | The Python package, copied from source.   |
| `/usr/bin/openkraken`                              | Launcher (adds that dir to `sys.path`).   |
| `/usr/share/applications/openkraken.desktop`       | Application-menu entry.                   |
| `/usr/share/icons/hicolor/scalable/apps/openkraken.svg` | App icon.                            |
| `/usr/lib/udev/rules.d/70-openkraken.rules`        | Non-root NZXT device access.              |
| `/usr/share/doc/openkraken/`                       | `README.md`, `PROTOCOL.md`.               |
| `/usr/share/licenses/openkraken/LICENSE`           | Licence (Arch convention).                 |

```
depends: python python-pyqt6 python-pillow liquidctl>=1.15 ttf-font
```

`ttf-font` is there because the LCD sensor screens draw text with Pillow, and
PIL's built-in bitmap font is unreadable at the 210 px temperature readout. Any
provider (`ttf-dejavu`, `ttf-liberation`, `noto-fonts`, ...) satisfies the
candidate chain in `openkraken/backend/lcd_render.py`.

Two deliberate choices worth knowing:

- **`arch=('any')`, not `x86_64`.** The `.deb` is `amd64` only because it
  vendors binary wheels (hidapi, Pillow). This package vendors nothing.
- **Code goes to `/usr/lib/openkraken`, not versioned `site-packages`.** That
  path does not move when Arch bumps Python 3.x, so a locally built package
  keeps working across interpreter updates instead of breaking until you
  remember to rebuild it. The launcher uses `#!/usr/bin/env python3` for the
  same reason.

Byte-compiled files are intentionally not shipped: they are tied to one
interpreter version, and a stale `.pyc` in `/usr` cannot be rewritten.

### Inspecting and removing

```sh
bsdtar -tf arch/dist/openkraken-*.pkg.tar.zst        # file listing
bsdtar -xOf arch/dist/openkraken-*.pkg.tar.zst .PKGINFO   # metadata
namcap arch/dist/openkraken-*.pkg.tar.zst            # optional lint
sudo pacman -R openkraken                            # remove
```

Arch already ships pacman hooks that reload udev rules and refresh the desktop
and icon caches, so `openkraken.install` deliberately does not duplicate them.
What the hooks do *not* do is re-apply rules to an already-plugged device — a
reload alone leaves an attached cooler on its old permissions — so the scriptlet
does a scoped `udevadm trigger`. It reloads first, because scriptlets run inside
the transaction, i.e. *before* the PostTransaction udev-reload hook.

## Which should I use?

- **apt-based distro?** Build and install the `.deb`.
- **pacman-based distro (Arch, Omarchy)?** Build and install the Arch package —
  it is smaller, vendors nothing, and every dependency is a real package.
- **Any other distro, or you want the latest source / an editable checkout?**
  Use the `install.sh` one-liner.

All three install the same application; they only differ in how the code and its
dependencies are laid out on disk.
