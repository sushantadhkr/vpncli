"""Updating an existing installation in place.

Every supported install layout (pipx, the private virtual environment from
``install.sh``, a plain ``pip install``) is produced by the install script,
so an update is just running that script again. The script is downloaded
over HTTPS from the repository, executed, and the application registration
is refreshed because the interpreter may have moved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from . import assets, paths

DEFAULT_REPO = "https://github.com/sushantadhkr/vpncli"
INSTALLER = {"posix": "install.sh", "win": "install.ps1"}
MAX_INSTALLER_BYTES = 2 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30.0
RUN_TIMEOUT = 900.0


class UpdateError(Exception):
    pass


def repo_url() -> str:
    return os.environ.get("VPNCLI_REPO") or DEFAULT_REPO


def owner_repo() -> Tuple[str, str]:
    """Pull ``owner/name`` out of the repository URL."""
    url = repo_url()
    url = url.replace("github.com:", "github.com/")
    if "github.com/" in url:
        tail = url.split("github.com/", 1)[1].strip("/")
    else:
        parts = url.rstrip("/").split("/")
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else ""
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    parts = [part for part in tail.split("/") if part]
    if len(parts) < 2:
        raise UpdateError(
            "cannot work out the repository from VPNCLI_REPO=%r; "
            "expected a github.com/owner/name URL" % url
        )
    return parts[0], parts[1]


def installer_url() -> str:
    owner, name = owner_repo()
    script = INSTALLER["win"] if paths.WINDOWS else INSTALLER["posix"]
    return "https://raw.githubusercontent.com/%s/%s/main/%s" % (owner, name, script)


def fetch_installer(url: Optional[str] = None) -> Path:
    """Download the install script to a temporary file and return its path."""
    url = url or installer_url()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "vpncli"})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            body = response.read(MAX_INSTALLER_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise UpdateError("could not download %s: %s" % (url, exc))
    if not body:
        raise UpdateError("the downloaded installer from %s is empty" % url)
    if len(body) > MAX_INSTALLER_BYTES:
        raise UpdateError("the downloaded installer from %s is too large" % url)
    target = paths.run_dir() / ("update-%s" % installer_url().rsplit("/", 1)[-1])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def installer_command(script: Path) -> List[str]:
    if paths.WINDOWS:
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    return ["/bin/sh", str(script)]


def run_installer(script: Path, no_gui: bool = False) -> Tuple[int, str]:
    environment = dict(os.environ)
    if no_gui:
        environment["VPNCLI_NO_GUI"] = "1"
    else:
        environment.pop("VPNCLI_NO_GUI", None)
    try:
        result = subprocess.run(
            installer_command(script),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=environment,
            timeout=RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise UpdateError("the installer did not finish within %d seconds" % int(RUN_TIMEOUT))
    except OSError as exc:
        raise UpdateError("could not run the installer: %s" % exc)
    return result.returncode, result.stdout or ""


def update(no_gui: bool = False, dry_run: bool = False) -> Tuple[str, Optional[str]]:
    """Download and run the installer again.

    Returns ``(installer_url, output)``. With ``dry_run`` only the URL is
    resolved and nothing is downloaded or executed.
    """
    url = installer_url()
    if dry_run:
        return url, None
    script = fetch_installer(url)
    try:
        code, output = run_installer(script, no_gui=no_gui)
    finally:
        paths.remove_quietly(script)
    if code != 0:
        raise UpdateError(
            "the installer failed with exit code %d:\n%s" % (code, output.strip() or "(no output)")
        )
    # The installer may have replaced the environment, so make sure the
    # launcher points at the interpreter that is actually in use now.
    try:
        assets.register(force=True)
    except Exception:
        pass
    return url, output