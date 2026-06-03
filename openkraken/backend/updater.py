"""Check GitHub for a newer OpenKraken and (for git checkouts) apply it.

Pure stdlib (``urllib``/``subprocess``) so it adds no dependency.  All network
and git work is best-effort and never raises out of the public functions; the
GUI runs them on a worker thread.

Update model:

* **git checkout** (the dev / ``install.sh`` layout): compare the local ``HEAD``
  to ``origin/main`` on GitHub.  :func:`apply_update` does ``git pull --ff-only``
  and the app then restarts.
* **anything else** (e.g. an installed ``.deb``): we can still *report* whether
  the upstream default branch has advanced, but :func:`apply_update` declines and
  points at the releases page instead.
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


@dataclass
class UpdateStatus:
    """Result of an update check."""

    checked: bool                 # the network check completed
    update_available: bool
    can_apply: bool               # True only for a git checkout we can pull
    local_rev: str | None         # short local commit (git checkout) else None
    remote_rev: str | None        # short upstream commit
    message: str                  # human-readable summary for the UI
    error: str | None = None      # set when the check itself failed


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
        # Not a git checkout (e.g. a .deb): we can't self-update, but we can
        # still compare the installed version to the newest GitHub release.
        from openkraken import __version__ as _installed

        latest = latest_release_version()
        if latest and _version_tuple(latest) > _version_tuple(_installed):
            return UpdateStatus(
                checked=True,
                update_available=True,
                can_apply=False,
                local_rev=None,
                remote_rev=latest,
                message=(
                    f"v{latest} is available (you have v{_installed}). Update via your "
                    "package manager or download it from the releases page."
                ),
            )
        return UpdateStatus(
            checked=True,
            update_available=False,
            can_apply=False,
            local_rev=None,
            remote_rev=latest or remote_short,
            message=f"OpenKraken v{_installed} is up to date.",
        )

    if local == remote:
        return UpdateStatus(
            checked=True,
            update_available=False,
            can_apply=True,
            local_rev=local[:7],
            remote_rev=remote_short,
            message="OpenKraken is up to date.",
        )
    return UpdateStatus(
        checked=True,
        update_available=True,
        can_apply=True,
        local_rev=local[:7],
        remote_rev=remote_short,
        message=f"Update available ({local[:7]} → {remote_short}).",
    )


def apply_update() -> tuple[bool, str]:
    """``git pull --ff-only`` the checkout; return (success, message). No raise."""
    repo = _repo_dir()
    if not _is_git_checkout(repo):
        return False, "Not a git checkout; update via your package manager."
    rc, out = _run_git(["pull", "--ff-only", "origin", _BRANCH], repo)
    if rc != 0:
        _LOGGER.warning("git pull failed: %s", out)
        return False, f"Update failed: {out.splitlines()[-1] if out else 'git pull error'}"
    _LOGGER.info("updated via git pull: %s", out.replace("\n", " ")[:200])
    return True, "Updated. Restart OpenKraken to run the new version."
