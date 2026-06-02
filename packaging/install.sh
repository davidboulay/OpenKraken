#!/usr/bin/env bash
#
# OpenKraken — universal installer.
#
# Curl-able one-liner:
#   curl -fsSL https://raw.githubusercontent.com/davidboulay/OpenKraken/main/packaging/install.sh | bash
#
# Clones (or updates) the OpenKraken source into
# ~/.local/share/openkraken-src and runs its idempotent ./setup.sh, which
# creates a venv, installs the app + deps, and adds a desktop launcher.
#
# This installer makes NO changes outside ~/.local/share/openkraken-src and the
# desktop-entry directory; it never needs root (setup.sh itself only *offers* a
# sudo udev rule, interactively, and never when piped).
#
set -euo pipefail

REPO_URL="https://github.com/davidboulay/OpenKraken"
SRC_DIR="${OPENKRAKEN_SRC_DIR:-$HOME/.local/share/openkraken-src}"

# --- pretty output ----------------------------------------------------------
# Colour only when stdout is a terminal (so piped logs stay clean).
if [ -t 1 ]; then
    C_STEP=$'\033[1;35m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
    C_ERR=$'\033[1;31m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""; C_BOLD=""; C_OFF=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$C_STEP" "$C_OFF" "$C_BOLD" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %sok%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '    %s!!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
err()  { printf '%serror:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; }

# --- detect the distro family for friendly dependency hints -----------------
distro_family() {
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case " ${ID:-} ${ID_LIKE:-} " in
            *" debian "*|*" ubuntu "*) echo "apt"; return ;;
            *" fedora "*|*" rhel "*|*" centos "*) echo "dnf"; return ;;
            *" arch "*|*" archlinux "*|*" manjaro "*) echo "pacman"; return ;;
        esac
    fi
    echo "unknown"
}

# Print the right install command for a set of native package names.
hint_install() {
    # $1 = apt names, $2 = dnf names, $3 = pacman names
    local apt_names="$1" dnf_names="$2" pacman_names="$3"
    case "$(distro_family)" in
        apt)    info "Try: sudo apt update && sudo apt install -y $apt_names" ;;
        dnf)    info "Try: sudo dnf install -y $dnf_names" ;;
        pacman) info "Try: sudo pacman -S --needed $pacman_names" ;;
        *)
            info "Install these with your package manager:"
            info "  Debian/Ubuntu: sudo apt install -y $apt_names"
            info "  Fedora/RHEL:   sudo dnf install -y $dnf_names"
            info "  Arch:          sudo pacman -S --needed $pacman_names"
            ;;
    esac
}

missing=0

step "OpenKraken installer"
info "source dir : $SRC_DIR"
info "repository : $REPO_URL"

# --- check prerequisites ----------------------------------------------------
step "Checking prerequisites"

if command -v git >/dev/null 2>&1; then
    ok "git found ($(git --version 2>/dev/null | head -1))"
else
    err "git is required but was not found."
    hint_install "git" "git" "git"
    missing=1
fi

if command -v python3 >/dev/null 2>&1; then
    PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
    ok "python3 found (version $PYVER)"
    # Require >= 3.10 (matches pyproject requires-python).
    if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
        err "Python 3.10 or newer is required (found $PYVER)."
        hint_install "python3" "python3" "python"
        missing=1
    fi
else
    err "python3 is required but was not found."
    hint_install "python3 python3-venv" "python3" "python"
    missing=1
fi

# python3-venv is a separate package on Debian/Ubuntu; check it explicitly so we
# fail with a clear hint rather than deep inside setup.sh.
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
        err "Python's venv/ensurepip module is unavailable."
        hint_install "python3-venv" "python3" "python"
        missing=1
    fi
fi

# PyQt6 is best provided by the distribution; setup.sh falls back to pip, but a
# native package is far smaller and faster. Inform (do not fail) if absent.
if command -v python3 >/dev/null 2>&1 && \
   ! python3 -c 'import PyQt6.QtWidgets' >/dev/null 2>&1; then
    warn "PyQt6 not found in the system Python."
    info "setup.sh can install it from PyPI, but a native package is recommended:"
    hint_install "python3-pyqt6" "python3-pyqt6" "python-pyqt6"
fi

if [ "$missing" -ne 0 ]; then
    err "Missing prerequisites (see hints above); aborting."
    exit 1
fi

# --- clone or update --------------------------------------------------------
step "Fetching OpenKraken source"
mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ]; then
    info "existing checkout found; updating"
    if git -C "$SRC_DIR" pull --ff-only; then
        ok "updated $SRC_DIR"
    else
        warn "git pull --ff-only failed (local changes?); using the existing checkout as-is"
    fi
elif [ -e "$SRC_DIR" ]; then
    err "$SRC_DIR exists but is not a git checkout."
    info "Move or remove it, or set OPENKRAKEN_SRC_DIR to a different path, then re-run."
    exit 1
else
    info "cloning into $SRC_DIR"
    git clone --depth 1 "$REPO_URL" "$SRC_DIR"
    ok "cloned $REPO_URL"
fi

# --- run setup.sh -----------------------------------------------------------
step "Running setup.sh"
if [ ! -f "$SRC_DIR/setup.sh" ]; then
    err "setup.sh not found in $SRC_DIR — is this the right repository?"
    exit 1
fi
chmod +x "$SRC_DIR/setup.sh" 2>/dev/null || true
# Run from inside the checkout (setup.sh locates itself, but be explicit).
( cd "$SRC_DIR" && bash ./setup.sh )

step "All done"
ok "OpenKraken installed from $SRC_DIR"
info "Launch \"OpenKraken\" from your application menu, or run:"
info "    $SRC_DIR/.venv/bin/openkraken"
