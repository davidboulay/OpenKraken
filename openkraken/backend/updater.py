"""Check GitHub for a newer OpenKraken and (for git checkouts) apply it.

Pure stdlib (``urllib``/``subprocess``) so it adds no dependency.  All network
and git work is best-effort and never raises out of the public functions; the
GUI runs them on a worker thread.

Update model:

* **git checkout** (the dev / ``install.sh`` layout): compare the local ``HEAD``
  to ``origin/main`` on GitHub.  :func:`apply_update` does ``git pull --ff-only``
  and the app then restarts.
* **distribution package** (a ``.deb`` on Debian/Ubuntu, a ``.pkg.tar.zst`` on
  Arch): compare the installed version to the newest GitHub release.  When that
  release carries an asset in the right format *and* PolicyKit is available,
  :func:`apply_update` downloads it and installs it with ``pkexec`` --
  ``apt-get install`` or ``pacman -U`` as appropriate.
* **anything else**: we can still *report* whether a newer version exists, but
  :func:`apply_update` declines and points at the package manager or the
  releases page instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

REPO = "davidboulay/OpenKraken"
_BRANCH = "main"
_API_LATEST_COMMIT = f"https://api.github.com/repos/{REPO}/commits/{_BRANCH}"
_API_LATEST_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
_TIMEOUT = 6.0

# Release-asset extension that each packaged flavour can install itself from.
# A flavour absent from this map can only ever report, never apply.
_FLAVOR_ASSET_SUFFIX = {
    "deb": ".deb",
    "pacman": ".pkg.tar.zst",
}


def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "OpenKraken-updater"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        _LOGGER.info("update check: GitHub request failed (%s)", exc)
        return None


def _version_tuple(text: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' / '1.2.3' into a comparable tuple; () if unparsable."""
    cleaned = (text or "").lstrip("vV").split("-")[0].split("+")[0]
    parts = cleaned.split(".")
    try:
        return tuple(int(p) for p in parts if p != "")
    except ValueError:
        return ()


def latest_release_version() -> str | None:
    """Tag of the newest published GitHub release (without the 'v'), or None."""
    payload = _get_json(_API_LATEST_RELEASE)
    if not payload:
        return None
    tag = payload.get("tag_name")
    return tag.lstrip("vV") if isinstance(tag, str) and tag else None


def _latest_release_info(asset_suffix: str | None = None) -> tuple[str | None, str | None]:
    """(version-without-v, download url of the first *asset_suffix* asset).

    ``asset_suffix`` of ``None`` (an unpackaged install) skips the asset hunt
    and returns the version only.
    """
    payload = _get_json(_API_LATEST_RELEASE)
    if not payload:
        return None, None
    tag = payload.get("tag_name")
    version = tag.lstrip("vV") if isinstance(tag, str) and tag else None
    asset_url = None
    if asset_suffix:
        for asset in payload.get("assets") or []:
            if str(asset.get("name", "")).endswith(asset_suffix):
                asset_url = asset.get("browser_download_url")
                break
    return version, asset_url


@dataclass
class UpdateStatus:
    """Result of an update check."""

    checked: bool                 # the network check completed
    update_available: bool
    can_apply: bool               # an in-app update path exists (git pull / .deb)
    local_rev: str | None         # short local commit (git checkout) else None
    remote_rev: str | None        # short upstream commit
    message: str                  # human-readable summary for the UI
    error: str | None = None      # set when the check itself failed
    latest_version: str | None = None  # newest release version, when known
    #: How this copy was installed: "git", "deb", "pacman" or "unknown".
    flavor: str = "unknown"
    #: Newest release's installable asset for this flavour (.deb / .pkg.tar.zst).
    asset_url: str | None = None


def _repo_dir() -> Path:
    """Directory of the installed package tree (…/openkraken/..)."""
    return Path(__file__).resolve().parent.parent.parent


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run ``git *args`` in *cwd*; return (returncode, combined output)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _is_git_checkout(repo: Path) -> bool:
    if not (repo / ".git").exists():
        return False
    rc, _ = _run_git(["rev-parse", "--is-inside-work-tree"], repo)
    return rc == 0


def _install_flavor() -> str:
    """How this copy of OpenKraken was installed.

    ``"git"`` (a checkout, incl. the ``install.sh`` / ``setup.sh`` layout),
    ``"pacman"`` or ``"deb"`` (a distribution package that owns our files), or
    ``"unknown"``.

    The package tests ask the package manager who owns *this very file* rather
    than sniffing ``/etc/os-release``: that is what actually decides which
    command can upgrade us.  A pacman-based distro with a source checkout must
    come out as ``"git"``, which an os-release check would get wrong.
    """
    import shutil

    if _is_git_checkout(_repo_dir()):
        return "git"

    me = str(Path(__file__).resolve())
    for flavor, cmd in (("pacman", ["pacman", "-Qo"]), ("deb", ["dpkg", "-S"])):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            proc = subprocess.run(
                [*cmd, me], capture_output=True, text=True, timeout=_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return flavor
    return "unknown"


def _local_head(repo: Path) -> str | None:
    rc, out = _run_git(["rev-parse", "HEAD"], repo)
    return out.strip() if rc == 0 and out.strip() else None


def _fetch_remote_head() -> str | None:
    """Latest commit SHA on the upstream default branch via the GitHub API."""
    req = urllib.request.Request(
        _API_LATEST_COMMIT,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "OpenKraken-updater"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        _LOGGER.info("update check: could not reach GitHub (%s)", exc)
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) and sha else None


def check_for_update() -> UpdateStatus:
    """Check GitHub for a newer commit; never raises."""
    repo = _repo_dir()
    is_git = _is_git_checkout(repo)
    local = _local_head(repo) if is_git else None

    remote = _fetch_remote_head()
    if remote is None:
        return UpdateStatus(
            checked=False,
            update_available=False,
            can_apply=False,
            local_rev=(local or "")[:7] or None,
            remote_rev=None,
            message="Couldn't reach GitHub to check for updates.",
            error="network",
        )

    remote_short = remote[:7]
    if not is_git or local is None:
        # A packaged install: compare the installed version to the newest
        # GitHub release. When that release carries an asset our package
        # manager understands and PolicyKit is available, we can self-update by
        # downloading it and installing via pkexec (Clippy-style).
        import shutil as _shutil

        from openkraken import __version__ as _installed

        flavor = _install_flavor()
        suffix = _FLAVOR_ASSET_SUFFIX.get(flavor)
        latest, asset_url = _latest_release_info(suffix)
        if latest and _version_tuple(latest) > _version_tuple(_installed):
            can_self_install = bool(asset_url) and _shutil.which("pkexec") is not None
            return UpdateStatus(
                checked=True,
                update_available=True,
                can_apply=can_self_install,
                local_rev=None,
                remote_rev=latest,
                message=(
                    f"v{latest} is available (you have v{_installed})."
                    + ("" if can_self_install else f" {_manual_update_hint(flavor)}")
                ),
                latest_version=latest,
                flavor=flavor,
                asset_url=asset_url,
            )
        return UpdateStatus(
            checked=True,
            update_available=False,
            can_apply=False,
            local_rev=None,
            remote_rev=latest or remote_short,
            message=f"OpenKraken v{_installed} is up to date.",
            latest_version=latest,
            flavor=flavor,
        )

    if local == remote:
        return UpdateStatus(
            checked=True,
            update_available=False,
            can_apply=True,
            local_rev=local[:7],
            remote_rev=remote_short,
            message="OpenKraken is up to date.",
            flavor="git",
        )
    return UpdateStatus(
        checked=True,
        update_available=True,
        can_apply=True,
        local_rev=local[:7],
        remote_rev=remote_short,
        message=f"Update available ({local[:7]} → {remote_short}).",
        flavor="git",
    )


def _manual_update_hint(flavor: str) -> str:
    """One sentence telling the user how to update this kind of install."""
    if flavor == "pacman":
        return (
            "Update with your package manager, or rebuild from "
            "packaging/arch/ in an updated checkout."
        )
    if flavor == "deb":
        return "Update with `sudo apt upgrade`, or see the releases page."
    return "Update via your package manager or the releases page."


def _download_asset(url: str, suffix: str, timeout: float = 180.0) -> str | None:
    """Download a release asset to a temp file; return its path or None."""
    import shutil
    import tempfile

    fd, path = tempfile.mkstemp(prefix="openkraken-update-", suffix=suffix)
    try:
        import os

        os.close(fd)
        req = urllib.request.Request(url, headers={"User-Agent": "OpenKraken-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        return path
    except (urllib.error.URLError, OSError) as exc:
        _LOGGER.warning("update download failed: %s", exc)
        return None


# Root command that installs a downloaded package, per flavour. Both are
# non-interactive and accept a downgrade, so re-installing the same or an older
# version (a rollback) works the same way an upgrade does.
_FLAVOR_INSTALL_CMD = {
    "deb": ["apt-get", "install", "-y", "--allow-downgrades"],
    "pacman": ["pacman", "-U", "--noconfirm"],
}


def _install_package(path: str, flavor: str, timeout: float = 300.0) -> tuple[bool, str]:
    """Install a downloaded package as root via PolicyKit.

    ``pkexec`` raises the desktop's authentication dialog; a dismissed or failed
    prompt comes back as exit code 126/127, which we report as a cancellation
    rather than a package-manager failure.
    """
    import shutil

    argv = _FLAVOR_INSTALL_CMD.get(flavor)
    if argv is None:
        return False, f"Don't know how to install a package for a {flavor!r} install."
    if shutil.which("pkexec") is None:
        return False, "pkexec (PolicyKit) is not available."
    try:
        proc = subprocess.run(
            ["pkexec", *argv, path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "Update installed. Restart OpenKraken to run the new version."
    if proc.returncode in (126, 127):  # dialog dismissed / auth failed
        return False, "Authentication cancelled."
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, detail[-1] if detail else f"{argv[0]} exited {proc.returncode}"


def _icon_path() -> str:
    """Path of the packaged app icon (for notifications), or a themed name."""
    icon = _repo_dir() / "openkraken" / "resources" / "openkraken-mark.png"
    return str(icon) if icon.exists() else "openkraken"


def notify_update(status: UpdateStatus, timeout: float = 600.0) -> str | None:
    """Desktop notification that an update is available; returns the action.

    Uses ``notify-send`` action buttons when supported (COSMIC/GNOME do):
    **Update now** (only when ``status.can_apply``) and **Release notes**.
    Blocks until the user clicks or dismisses (``-A`` implies ``--wait``), so
    call it from a worker thread.  Returns ``"update"``, ``"open"``, or ``None``
    (dismissed / no notify-send / no action support — a plain notification is
    shown instead when possible).  Never raises.
    """
    import shutil

    if shutil.which("notify-send") is None:
        return None
    latest = status.latest_version or status.remote_rev or "a new version"
    title = f"OpenKraken {latest} is available"
    from openkraken import __version__ as _installed

    body = f"You have {_installed}."
    try:
        help_txt = subprocess.run(
            ["notify-send", "--help"], capture_output=True, text=True, timeout=5
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    if "--action" not in help_txt:
        try:  # informational fallback: no buttons available
            subprocess.Popen(
                ["notify-send", "--app-name", "OpenKraken", "--icon", _icon_path(),
                 title, f"{body} Open Settings → Check for updates."],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass
        return None
    cmd = ["notify-send", "--app-name", "OpenKraken", "--icon", _icon_path()]
    if status.can_apply:
        cmd += ["--action", "update=Update now"]
    cmd += ["--action", "open=Release notes", title, body]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    key = (proc.stdout or "").strip()
    if key == "open":
        _open_release_page()
        return "open"
    return key or None


def _open_release_page() -> None:
    import shutil

    if shutil.which("xdg-open") is None:
        return
    try:
        subprocess.Popen(
            ["xdg-open", RELEASES_URL], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def apply_update(status: UpdateStatus | None = None) -> tuple[bool, str]:
    """Apply the available update; return (success, message). Never raises.

    * git checkout -> ``git pull --ff-only`` (unchanged historic path);
    * packaged install -> download the newest release's asset for this flavour
      (``.deb`` on Debian/Ubuntu, ``.pkg.tar.zst`` on Arch) and install it via
      ``pkexec`` (PolicyKit password dialog), Clippy-style.

    ``status`` (from :func:`check_for_update`) supplies the flavour and asset
    url; when omitted both are re-derived.
    """
    repo = _repo_dir()
    if _is_git_checkout(repo):
        rc, out = _run_git(["pull", "--ff-only", "origin", _BRANCH], repo)
        if rc != 0:
            _LOGGER.warning("git pull failed: %s", out)
            return False, f"Update failed: {out.splitlines()[-1] if out else 'git pull error'}"
        _LOGGER.info("updated via git pull: %s", out.replace("\n", " ")[:200])
        return True, "Updated. Restart OpenKraken to run the new version."

    flavor = status.flavor if status is not None and status.flavor != "unknown" else _install_flavor()
    suffix = _FLAVOR_ASSET_SUFFIX.get(flavor)
    if suffix is None:
        return False, _manual_update_hint(flavor)

    asset_url = status.asset_url if status is not None else None
    if not asset_url:
        _, asset_url = _latest_release_info(suffix)
    if not asset_url:
        return False, (
            f"The latest release ships no {suffix} asset. "
            f"{_manual_update_hint(flavor)}"
        )
    path = _download_asset(asset_url, suffix)
    if path is None:
        return False, "Could not download the update."
    ok, msg = _install_package(path, flavor)
    try:  # best-effort temp cleanup either way
        import os

        os.unlink(path)
    except OSError:
        pass
    return ok, msg
