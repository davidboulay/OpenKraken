#!/usr/bin/env bash
#
# Kraken CAM — environment bootstrap.
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

DESKTOP_SRC="$SCRIPT_DIR/kraken-cam.desktop"
DESKTOP_DST_DIR="$HOME/.local/share/applications"
DESKTOP_DST="$DESKTOP_DST_DIR/kraken-cam.desktop"

EXEC_PATH="$VENV_DIR/bin/kraken-cam"
ICON_PATH="$SCRIPT_DIR/krakencam/resources/kraken-cam.svg"

GIT_LIQUIDCTL="git+https://github.com/liquidctl/liquidctl"

# --- pretty progress --------------------------------------------------------
step() { printf '\n\033[1;35m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }

# --- 1. virtual environment -------------------------------------------------
step "Creating virtual environment (.venv, --system-site-packages)"
if [[ -x "$PY" ]]; then
    ok "venv already present at $VENV_DIR"
else
    python3 -m venv --system-site-packages "$VENV_DIR"
    ok "created $VENV_DIR"
fi

# --- 2. install project -----------------------------------------------------
step "Installing Kraken CAM (editable) and dependencies"
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

# --- 6. done ----------------------------------------------------------------
step "Setup complete"
cat <<EOF

  Kraken CAM is installed.

  Run it from a terminal:
      ${EXEC_PATH}

  Or launch "Kraken CAM" from your application menu.

  Useful flags:
      ${EXEC_PATH} --minimized     start hidden in the system tray
      ${EXEC_PATH} --debug         verbose logging
      ${EXEC_PATH} --version       print version and exit

EOF
