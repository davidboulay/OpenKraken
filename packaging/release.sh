#!/usr/bin/env bash
#
# release.sh — cut an OpenKraken release.
#
# Usage:
#   packaging/release.sh patch|minor|major     # bump from the current version
#   packaging/release.sh X.Y.Z                  # set an explicit version
#   packaging/release.sh --print                # just print the current version
#
# Steps it performs:
#   1. compute the new version and write it to pyproject.toml + openkraken/__init__.py
#   2. commit "Release vX.Y.Z" and create an annotated tag vX.Y.Z
#   3. push the branch and the tag
#   4. build the .deb (packaging/build-deb.sh)
#   5. create a GitHub release for the tag with the .deb attached (needs `gh`)
#
# The GitHub Actions workflow (.github/workflows/release.yml) ALSO builds and
# attaches a .deb on any pushed tag, so step 5 is belt-and-braces for local runs
# and a no-op-safe if a release already exists.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
INIT_PY="$ROOT/openkraken/__init__.py"
PYPROJECT="$ROOT/pyproject.toml"

err() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

current_version() {
    grep -oP '^__version__\s*=\s*"\K[0-9]+\.[0-9]+\.[0-9]+' "$INIT_PY"
}

[ -f "$INIT_PY" ] || err "cannot find $INIT_PY (run from the repo)"
CUR="$(current_version)" || err "could not read current version"

if [ "${1:-}" = "--print" ]; then echo "$CUR"; exit 0; fi
[ $# -eq 1 ] || err "usage: release.sh patch|minor|major|X.Y.Z|--print"

case "$1" in
    major|minor|patch)
        IFS=. read -r MA MI PA <<<"$CUR"
        case "$1" in
            major) MA=$((MA + 1)); MI=0; PA=0 ;;
            minor) MI=$((MI + 1)); PA=0 ;;
            patch) PA=$((PA + 1)) ;;
        esac
        NEW="$MA.$MI.$PA"
        ;;
    [0-9]*.[0-9]*.[0-9]*) NEW="$1" ;;
    *) err "invalid version/bump: $1" ;;
esac

info "Releasing v$NEW (was v$CUR)"

# --- preflight ---------------------------------------------------------------
[ -z "$(git -C "$ROOT" status --porcelain)" ] || err "working tree is dirty; commit or stash first"
git -C "$ROOT" rev-parse "v$NEW" >/dev/null 2>&1 && err "tag v$NEW already exists"

# --- 1. write the new version ------------------------------------------------
sed -i -E "s/^__version__ = \"[0-9.]+\"/__version__ = \"$NEW\"/" "$INIT_PY"
sed -i -E "0,/^version = \"[0-9.]+\"/s//version = \"$NEW\"/" "$PYPROJECT"
info "version bumped in __init__.py + pyproject.toml"

# --- 2. commit + tag ---------------------------------------------------------
git -C "$ROOT" add openkraken/__init__.py pyproject.toml
git -C "$ROOT" commit -q -m "Release v$NEW"
git -C "$ROOT" tag -a "v$NEW" -m "OpenKraken v$NEW"
info "committed and tagged v$NEW"

# --- 3. push -----------------------------------------------------------------
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
git -C "$ROOT" push origin "$BRANCH"
git -C "$ROOT" push origin "v$NEW"
info "pushed $BRANCH and tag v$NEW"

# --- 4. build the .deb -------------------------------------------------------
info "building the .deb"
"$SCRIPT_DIR/build-deb.sh"
DEB="$(ls -t "$SCRIPT_DIR/dist/openkraken_${NEW}_"*.deb 2>/dev/null | head -1 || true)"
[ -n "$DEB" ] || err "build-deb.sh did not produce openkraken_${NEW}_*.deb"
info "built $DEB"

# --- 5. GitHub release -------------------------------------------------------
if command -v gh >/dev/null 2>&1; then
    info "creating GitHub release v$NEW"
    if gh release view "v$NEW" >/dev/null 2>&1; then
        gh release upload "v$NEW" "$DEB" --clobber
    else
        gh release create "v$NEW" "$DEB" \
            --title "OpenKraken v$NEW" \
            --generate-notes
    fi
    info "GitHub release v$NEW ready"
else
    info "gh not found — tag pushed; the GitHub Actions workflow will build the release."
fi

# --- 6. APT repository (GitHub Pages) -----------------------------------------
# Rebuild the flat, GPG-signed APT repo from ALL release .debs and force-push it
# to gh-pages. The signing key lives ONLY on this machine
# (~/.config/openkraken-apt/gnupg; backup in ~/.local/bin/.openkraken-bak/
# apt-signing/) — CI's apt-repo.yml is an optional mirror that stays dormant
# unless an APT_GPG_PRIVATE_KEY repo secret is configured.
APT_GNUPGHOME="$HOME/.config/openkraken-apt/gnupg"
if [ -d "$APT_GNUPGHOME" ] && command -v gh >/dev/null 2>&1 \
        && command -v dpkg-scanpackages >/dev/null 2>&1 \
        && command -v apt-ftparchive >/dev/null 2>&1; then
    info "publishing the APT repository to gh-pages"
    APT_TMP="$(mktemp -d)"
    trap 'rm -rf "$APT_TMP"' EXIT
    mkdir -p "$APT_TMP/debs"
    for tag in $(gh release list --limit 100 --json tagName -q '.[].tagName'); do
        gh release download "$tag" --pattern '*.deb' --dir "$APT_TMP/debs" \
            --skip-existing 2>/dev/null || true
    done
    GNUPGHOME="$APT_GNUPGHOME" \
        PAGES_URL="https://davidboulay.github.io/OpenKraken" \
        "$SCRIPT_DIR/apt/build-repo.sh" "$APT_TMP/debs" "$APT_TMP/public"
    touch "$APT_TMP/public/.nojekyll"
    (
        cd "$APT_TMP/public"
        git init -q
        git checkout -q -b gh-pages
        git add -A
        git -c user.name="davidboulay" \
            -c user.email="89959743+davidboulay@users.noreply.github.com" \
            commit -qm "APT repo: v$NEW ($(date -u +%FT%TZ))"
        git push -f "https://github.com/davidboulay/OpenKraken" gh-pages
    )
    info "APT repository published (davidboulay.github.io/OpenKraken)"
else
    info "APT publish skipped (signing keyring or dpkg-dev/apt-utils/gh missing)."
fi

info "Done: v$NEW released."
