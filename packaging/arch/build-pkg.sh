#!/usr/bin/env bash
#
# build-pkg.sh — build openkraken-<version>-<rel>-any.pkg.tar.zst with makepkg.
#
# The Arch counterpart of ../build-deb.sh. It keeps PKGBUILD's pkgver in sync
# with pyproject.toml, then runs makepkg out of this directory (the PKGBUILD
# packages the surrounding checkout, so it builds your working tree).
#
# Unlike the .deb build this needs no network: nothing is vendored, because
# Arch's own repositories carry liquidctl >= 1.16 (with Kraken 2024 support),
# python-pyqt6 and python-pillow.
#
#   ./build-pkg.sh              build only
#   ./build-pkg.sh --install    build, then install with pacman (prompts)
#   ./build-pkg.sh --force      rebuild even if the package already exists
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
PKGBUILD_FILE="$SCRIPT_DIR/PKGBUILD"

# Land packages in packaging/arch/dist/, mirroring packaging/dist/ for .debs.
export PKGDEST="${PKGDEST:-$SCRIPT_DIR/dist}"

step() { printf '\n\033[1;35m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

MAKEPKG_ARGS=()
DO_INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install|-i) DO_INSTALL=1 ;;
        --force|-f)   MAKEPKG_ARGS+=(--force) ;;
        --help|-h)
            sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done

# --- sanity checks ----------------------------------------------------------
command -v makepkg >/dev/null 2>&1 || die "makepkg not found (install the 'base-devel' group)"
[[ -f "$PKGBUILD_FILE" ]]              || die "PKGBUILD not found at $PKGBUILD_FILE"
[[ -f "$PROJECT_ROOT/pyproject.toml" ]] || die "pyproject.toml not found at $PROJECT_ROOT"
[[ -d "$PROJECT_ROOT/openkraken" ]]     || die "openkraken/ package not found at $PROJECT_ROOT"
[[ "$(id -u)" -ne 0 ]]                  || die "do not run makepkg as root; run as your normal user"

# --- keep pkgver in sync with pyproject.toml --------------------------------
step "Syncing pkgver with pyproject.toml"
VERSION="$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$PROJECT_ROOT/pyproject.toml" | head -1)"
[[ -n "$VERSION" ]] || die "could not read version from pyproject.toml"
CURRENT="$(sed -n 's/^pkgver=\(.*\)$/\1/p' "$PKGBUILD_FILE" | head -1)"
if [[ "$CURRENT" == "$VERSION" ]]; then
    ok "pkgver already $VERSION"
else
    # A new upstream version restarts the package release counter at 1.
    sed -i -e "s/^pkgver=.*/pkgver=$VERSION/" -e "s/^pkgrel=.*/pkgrel=1/" "$PKGBUILD_FILE"
    ok "pkgver $CURRENT -> $VERSION (pkgrel reset to 1)"
    info "commit the PKGBUILD change alongside the version bump"
fi

# --- build ------------------------------------------------------------------
step "Building with makepkg"
info "project root : $PROJECT_ROOT"
info "output dir   : $PKGDEST"
mkdir -p "$PKGDEST"
# --cleanbuild so a stale $srcdir/$pkgdir can never leak into the package.
( cd "$SCRIPT_DIR" && makepkg --cleanbuild --clean "${MAKEPKG_ARGS[@]+"${MAKEPKG_ARGS[@]}"}" )

PKG_FILE="$(find "$PKGDEST" -maxdepth 1 -name "openkraken-${VERSION}-*.pkg.tar.*" \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
[[ -n "$PKG_FILE" ]] || die "build reported success but no package was found in $PKGDEST"

step "Done"
info "built: $PKG_FILE"
info "size : $(du -h "$PKG_FILE" | cut -f1)"

if [[ "$DO_INSTALL" -eq 1 ]]; then
    step "Installing with pacman"
    # pkexec keeps this working on setups where sudo wants a password on a tty
    # the GUI installer does not have; fall back to sudo when it is absent.
    if command -v pkexec >/dev/null 2>&1; then
        pkexec pacman -U --noconfirm "$PKG_FILE"
    else
        sudo pacman -U "$PKG_FILE"
    fi
    ok "installed"
else
    info "install with: pacman -U '$PKG_FILE'"
fi
printf '%s\n' "$PKG_FILE"
