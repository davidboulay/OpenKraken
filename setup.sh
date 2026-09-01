#!/usr/bin/env bash
#
# OpenKraken — environment bootstrap.
#
# Creates a virtual environment (with access to the system PyQt6), installs the
# project in editable mode, makes sure the liquidctl driver knows about the
# Kraken 2024 Elite RGB (USB 1e71:3012), and installs a desktop launcher.
#
# Safe to re-run: every step is idempotent.
#
set -euo pipefail

# --- locate ourselves -------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

DESKTOP_SRC="$SCRIPT_DIR/openkraken.desktop"
DESKTOP_DST_DIR="$HOME/.local/share/applications"
DESKTOP_DST="$DESKTOP_DST_DIR/openkraken.desktop"
# Stale pre-rename desktop entry to remove if present.
DESKTOP_STALE="$DESKTOP_DST_DIR/kraken-cam.desktop"

EXEC_PATH="$VENV_DIR/bin/openkraken"
ICON_PATH="$SCRIPT_DIR/openkraken/resources/openkraken.svg"

GIT_LIQUIDCTL="git+https://github.com/liquidctl/liquidctl"

# NZXT vendor id and where the device-access udev rule is installed.
NZXT_VENDOR_ID="1e71"
UDEV_RULE_PATH="/etc/udev/rules.d/70-openkraken.rules"

# TWO rules are required: hidraw covers status/cooling/lighting (HID reports),
# and the raw-usb rule covers the LCD, which liquidctl drives over a separate
# USB *bulk* interface via pyusb — without it enumeration itself crashes with
# "The device has no langid (permission issue...)" (issue #4).
#
# The GROUP fallback is distro-dependent. Debian/Ubuntu have a plugdev group and
# conventionally use it for removable-device access; Arch does not, and naming a
# group that does not exist makes udev log an error for every matching device.
# TAG+="uaccess" is what actually grants access on any systemd-logind desktop
# (an ACL for the user of the active local seat), so the group is only ever a
# fallback for systems without seat management.
if [[ -r /etc/os-release ]] && grep -qE '^(ID|ID_LIKE)=.*\b(arch|archlinux)\b' /etc/os-release; then
    UDEV_RULE_BODY='# OpenKraken — non-root access to NZXT coolers (HID + USB bulk for the LCD).
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1e71", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1e71", TAG+="uaccess"'
else
    UDEV_RULE_BODY='# OpenKraken — non-root access to NZXT coolers (HID + USB bulk for the LCD).
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1e71", TAG+="uaccess", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1e71", TAG+="uaccess", MODE="0660", GROUP="plugdev"'
fi

# --- pretty progress --------------------------------------------------------
step() { printf '\n\033[1;35m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!!\033[0m %s\n' "$*"; }

# --- 1. virtual environment -------------------------------------------------
step "Creating virtual environment (.venv, --system-site-packages)"
if [[ -x "$PY" ]]; then
    ok "venv already present at $VENV_DIR"
else
    python3 -m venv --system-site-packages "$VENV_DIR"
    ok "created $VENV_DIR"
fi

# --- 2. install project -----------------------------------------------------
step "Installing OpenKraken (editable) and dependencies"
"$PIP" install -U pip
"$PIP" install -e .
ok "package installed"

# --- 3. liquidctl: ensure Kraken 2024 (0x3012) is supported -----------------
step "Verifying liquidctl Kraken support"
"$PY" -c "from liquidctl.driver.kraken3 import KrakenZ3"
ok "liquidctl.driver.kraken3.KrakenZ3 importable"

KRAKEN3_FILE="$("$PY" -c 'import liquidctl.driver.kraken3 as m; print(m.__file__)')"
info "driver file: $KRAKEN3_FILE"
if grep -q "0x3012" "$KRAKEN3_FILE"; then
    ok "Kraken 2024 Elite RGB (1e71:3012) is supported by installed liquidctl"
else
    info "installed liquidctl lacks 0x3012 — upgrading from upstream git"
    "$PIP" install -U "liquidctl @ ${GIT_LIQUIDCTL}"
    KRAKEN3_FILE="$("$PY" -c 'import liquidctl.driver.kraken3 as m; print(m.__file__)')"
    if grep -q "0x3012" "$KRAKEN3_FILE"; then
        ok "upgraded; 0x3012 now supported"
    else
        printf '    \033[1;31m!!\033[0m %s\n' \
            "0x3012 still not found in $KRAKEN3_FILE after upgrade — device may not be detected."
    fi
fi

# --- 4. PyQt6 ---------------------------------------------------------------
step "Verifying PyQt6 availability"
if "$PY" -c "import PyQt6.QtWidgets" >/dev/null 2>&1; then
    ok "PyQt6 importable from the venv (system or installed)"
else
    info "PyQt6 not visible — installing into the venv"
    "$PIP" install "PyQt6>=6.4"
    "$PY" -c "import PyQt6.QtWidgets"
    ok "PyQt6 installed"
fi

# --- 5. desktop launcher ----------------------------------------------------
step "Installing desktop launcher"
mkdir -p "$DESKTOP_DST_DIR"
# Remove the stale pre-rename launcher so the menu shows only "OpenKraken".
if [[ -f "$DESKTOP_STALE" ]]; then
    rm -f "$DESKTOP_STALE"
    ok "removed stale launcher $DESKTOP_STALE"
fi
# Rewrite the Exec/Icon placeholders to absolute paths for the installed copy.
# Use '|' as the sed delimiter since the values are filesystem paths.
sed -e "s|@EXEC@|${EXEC_PATH}|g" \
    -e "s|@ICON@|${ICON_PATH}|g" \
    "$DESKTOP_SRC" >"$DESKTOP_DST"
chmod 644 "$DESKTOP_DST"
ok "installed $DESKTOP_DST"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DST_DIR" >/dev/null 2>&1 || true
    ok "refreshed desktop database"
fi
if [[ ! -f "$ICON_PATH" ]]; then
    info "note: icon not found at $ICON_PATH (the menu entry will use a fallback icon)"
fi

# --- 6. device access (udev rule) -------------------------------------------
step "Checking Kraken device access (/dev/hidraw* permissions)"

# Probe for a readable+writable hidraw belonging to NZXT (vendor 1e71).  We walk
# /sys/class/hidraw/*/device/uevent looking for a HID_ID line whose vendor field
# is 1E71, then os.access() the matching /dev/hidrawN for R_OK|W_OK.  Exit codes:
#   0 = an NZXT hidraw is present AND read/writable by us (access OK)
#   2 = no NZXT hidraw node is present at all (device unplugged / different host)
#   1 = an NZXT hidraw is present but NOT accessible (need the udev rule)
device_access_probe() {
    # Checks BOTH access paths liquidctl needs: the hidraw node (status /
    # cooling / lighting) AND the raw usbfs node (LCD bulk uploads + string
    # descriptors during enumeration — inaccessible => "no langid" crash).
    "$PY" - "$NZXT_VENDOR_ID" <<'PY'
import glob, os, sys

vendor = sys.argv[1].upper()

# --- hidraw side ---
hid_found = False
hid_ok = False
for uevent in glob.glob("/sys/class/hidraw/*/device/uevent"):
    try:
        with open(uevent, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        continue
    # HID_ID lines look like: HID_ID=0003:00001E71:00003012
    hid_id = ""
    for line in text.splitlines():
        if line.startswith("HID_ID="):
            hid_id = line.split("=", 1)[1].upper()
            break
    if not hid_id or vendor not in hid_id:
        continue
    hid_found = True
    # /sys/class/hidraw/hidrawN/device/uevent -> /dev/hidrawN
    name = uevent.split("/")[4]            # "hidrawN"
    if os.access("/dev/" + name, os.R_OK | os.W_OK):
        hid_ok = True
        break

# --- raw usb (usbfs) side ---
usb_found = False
usb_ok = False
for vid_file in glob.glob("/sys/bus/usb/devices/*/idVendor"):
    try:
        with open(vid_file, "r", encoding="ascii") as fh:
            if fh.read().strip().upper() != vendor:
                continue
    except OSError:
        continue
    usb_found = True
    d = os.path.dirname(vid_file)
    try:
        with open(os.path.join(d, "busnum")) as fh:
            bus = int(fh.read())
        with open(os.path.join(d, "devnum")) as fh:
            dev = int(fh.read())
    except (OSError, ValueError):
        continue
    if os.access("/dev/bus/usb/%03d/%03d" % (bus, dev), os.R_OK | os.W_OK):
        usb_ok = True
        break

found_device = hid_found or usb_found
accessible = hid_ok and (usb_ok or not usb_found)

if accessible:
    sys.exit(0)
sys.exit(1 if found_device else 2)
PY
}

install_udev_rule() {
    # Returns 0 on success, non-zero on any failure (caller prints manual steps).
    printf '%s\n' "$UDEV_RULE_BODY" | sudo tee "$UDEV_RULE_PATH" >/dev/null || return 1
    sudo udevadm control --reload-rules || return 1
    sudo udevadm trigger || return 1
    return 0
}

print_manual_udev() {
    info "To grant non-root access to the Kraken, create $UDEV_RULE_PATH containing:"
    info ""
    while IFS= read -r _rule_line; do
        info "    $_rule_line"
    done <<<"$UDEV_RULE_BODY"
    info ""
    info "then run:"
    info "    sudo udevadm control --reload-rules && sudo udevadm trigger"
    info "and re-plug the cooler (or reboot)."
}

set +e
device_access_probe
probe_rc=$?
set -e

if [[ "$probe_rc" -eq 0 ]]; then
    ok "device access OK (NZXT hidraw + usb device nodes read/writable by $USER)"
elif [[ ! -t 0 ]]; then
    # Non-interactive (piped, CI, etc.): never prompt, never fail — just note it.
    if [[ "$probe_rc" -eq 2 ]]; then
        info "no NZXT (1e71) hidraw detected; skipping the udev-rule prompt (not a TTY)"
    else
        warn "NZXT device present but not accessible; skipping the udev-rule prompt (not a TTY)"
    fi
    print_manual_udev
else
    if [[ "$probe_rc" -eq 2 ]]; then
        info "no NZXT (1e71) hidraw detected (device unplugged, or a different host)."
    else
        info "an NZXT device is present but not read/writable by $USER."
    fi
    if [[ -f "$UDEV_RULE_PATH" ]]; then
        info "a rule already exists at $UDEV_RULE_PATH; leaving it untouched."
        print_manual_udev
    else
        printf '    Install a udev rule at %s now (needs sudo)? [y/N] ' "$UDEV_RULE_PATH"
        read -r reply || reply=""
        case "$reply" in
            [yY] | [yY][eE][sS])
                if install_udev_rule; then
                    ok "installed $UDEV_RULE_PATH and reloaded udev (re-plug the cooler to apply)"
                else
                    warn "could not install the udev rule (sudo declined or failed)."
                    print_manual_udev
                fi
                ;;
            *)
                info "skipped udev-rule install (you can add it later)."
                print_manual_udev
                ;;
        esac
    fi
fi

# --- 7. done ----------------------------------------------------------------
step "Setup complete"
cat <<EOF

  OpenKraken is installed.

  Run it from a terminal:
      ${EXEC_PATH}

  Or launch "OpenKraken" from your application menu.

  Useful flags:
      ${EXEC_PATH} --minimized     start hidden in the system tray
      ${EXEC_PATH} --debug         verbose logging
      ${EXEC_PATH} --version       print version and exit

EOF
