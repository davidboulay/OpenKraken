#!/usr/bin/env bash
# Build a *flat* APT repository from one or more .deb files and GPG-sign it.
# (Adapted from Clippy's packaging/apt/build-repo.sh.)
#
#   ./packaging/apt/build-repo.sh <deb-dir> <out-dir>
#
# Produces, in <out-dir>: the .deb(s), Packages(.gz), a signed Release
# (Release.gpg + InRelease), the public signing key (openkraken.gpg), and a
# small index.html. Requires: dpkg-dev (dpkg-scanpackages), apt-utils
# (apt-ftparchive), gnupg — and a secret signing key already imported into the
# active GNUPGHOME.
#
# Users consume it with:
#   deb [signed-by=/usr/share/keyrings/openkraken.gpg] https://<pages-url> ./
set -euo pipefail

DEB_DIR="${1:?usage: build-repo.sh <deb-dir> <out-dir>}"
OUT="${2:?usage: build-repo.sh <deb-dir> <out-dir>}"
PAGES_URL="${PAGES_URL:-https://davidboulay.github.io/OpenKraken}"

rm -rf "$OUT"; mkdir -p "$OUT"
cp "$DEB_DIR"/*.deb "$OUT"/ 2>/dev/null || { echo "no .deb files in $DEB_DIR" >&2; exit 1; }

# Public key so users can trust the repo (dearmored keyring for signed-by).
gpg --export  > "$OUT/openkraken.gpg"
gpg --export --armor > "$OUT/openkraken.asc"

cd "$OUT"
# Package index (relative Filename: ./openkraken_*.deb — a flat repo).
dpkg-scanpackages --multiversion . /dev/null > Packages
gzip -9c Packages > Packages.gz

# Release file with checksums of the index, then detached + inline signatures.
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=OpenKraken \
  -o APT::FTPArchive::Release::Label=OpenKraken \
  -o APT::FTPArchive::Release::Suite=stable \
  -o APT::FTPArchive::Release::Codename=stable \
  -o APT::FTPArchive::Release::Architectures=amd64 \
  -o APT::FTPArchive::Release::Components=main \
  release . > Release
gpg --batch --yes --armor --detach-sign -o Release.gpg Release
gpg --batch --yes --clearsign -o InRelease Release

cat > index.html <<HTML
<!doctype html><meta charset="utf-8"><title>OpenKraken APT repository</title>
<body style="font-family:system-ui;max-width:44rem;margin:3rem auto;padding:0 1rem;line-height:1.5">
<h1>OpenKraken — APT repository</h1>
<p>Control center for NZXT Kraken AIO liquid coolers on Linux. Install and stay updated via apt:</p>
<pre style="background:#f4f4f4;padding:1rem;overflow:auto">curl -fsSL $PAGES_URL/openkraken.gpg | sudo tee /usr/share/keyrings/openkraken.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/openkraken.gpg] $PAGES_URL ./" | sudo tee /etc/apt/sources.list.d/openkraken.list
sudo apt update &amp;&amp; sudo apt install openkraken</pre>
<p>Source &amp; releases: <a href="https://github.com/davidboulay/OpenKraken">github.com/davidboulay/OpenKraken</a></p>
</body>
HTML

echo "built flat APT repo in $OUT:"; ls -1
