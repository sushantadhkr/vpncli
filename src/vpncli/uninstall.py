"""Removal of both the installed package (CLI + tray GUI) and local data.

The tray is not a separate product: ``vpncli`` (console script) and
``vpncli-tray`` (GUI script) are two entry points of one distribution, so
every supported install layout can be removed with the same plan.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import paths

PACKAGE = "vpncli"
WAIT_SECONDS = 2

#: A removal step: a human label plus the command that performs it.
Step = Tuple[str, List[str]]


class UninstallError(Exception):
    pass


def _local_dir(home: Path) -> Path:
    return home / ".local"


def _venv_dir(home: Path) -> Path:
    return _local_dir(home) / "share" / PACKAGE / "venv"


def _pipx_venvs(home: Path) -> List[Path]:
    """Every location pipx may use, XDG-style and legacy alike."""
    return [
        home / ".local" / "share" / "pipx" / "venvs" / PACKAGE,
        home / ".local" / "pipx" / "venvs" / PACKAGE,
    ]


def _bin_link(home: Path) -> Path:
    return _local_dir(home) / "bin" / PACKAGE


def _installed_in_current_env() -> bool:
    try:
        from importlib import metadata
    except ImportError:
        return False
    try:
        metadata.distribution(PACKAGE)
    except Exception:
        return False
    return True


def _prefix() -> Path:
    return Path(sys.prefix).resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        return Path(os.path.commonpath([str(path), str(parent)])) == Path(parent)
    except ValueError:
        return False


def _remove_tree(path: Path) -> List[str]:
    if paths.WINDOWS:
        return ["rmdir", "/s", "/q", str(path)]
    return ["rm", "-rf", str(path)]


def plan(home: Optional[Path] = None) -> List[Step]:
    """Work out how this copy of vpncli was installed and how to remove it.

    Detection is based on the on-disk layout produced by ``install.sh`` /
    ``install.ps1``, plus the interpreter currently running. Only layouts
    that actually exist are returned, so a step never fails on a machine
    that used a different installer.
    """
    home = home if home is not None else paths.invoking_home()
    prefix = _prefix()
    steps: List[Step] = []

    pipx_venvs = [candidate for candidate in _pipx_venvs(home) if candidate.is_dir()]
    if pipx_venvs:
        if not shutil.which("pipx"):
            raise UninstallError(
                "pipx is not on PATH but its environment still exists at %s; "
                "remove that folder by hand" % ", ".join(str(p) for p in pipx_venvs)
            )
        steps.append(
            (
                "pipx environment (%s) - removes the %s and vpncli-tray commands"
                % (", ".join(str(p) for p in pipx_venvs), PACKAGE),
                ["pipx", "uninstall", PACKAGE],
            )
        )
    elif _venv_dir(home).is_dir():
        steps.append(
            (
                "installation folder (%s)" % (_local_dir(home) / "share" / PACKAGE),
                _remove_tree(_local_dir(home) / "share" / PACKAGE),
            )
        )
        if _bin_link(home).exists():
            steps.append(("PATH entry (%s)" % _bin_link(home), _remove_tree(_bin_link(home))))

    if _installed_in_current_env() and not any(
        _inside(prefix, candidate)
        for candidate in list(pipx_venvs) + [_venv_dir(home)]
    ):
        steps.append(
            (
                "python package in %s" % prefix,
                [sys.executable, "-m", "pip", "uninstall", "-y", PACKAGE],
            )
        )

    if not steps:
        raise UninstallError(
            "could not find an installed %s under %s; remove it with the "
            "installer you used" % (PACKAGE, home)
        )
    return steps


def data_dir() -> Path:
    return paths.data_dir()


def remove_data() -> Path:
    """Delete the data directory, including saved credentials and logs."""
    target = data_dir()
    if len(target.parts) < 3:
        # Never delete something shallow like /Users or C:\.
        raise UninstallError("refusing to remove %s" % target)
    if not target.exists():
        return target
    if target.is_dir():
        shutil.rmtree(str(target))
    else:
        paths.remove_quietly(target)
    return target


def _spawn(command: List[str]) -> None:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if paths.WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        subprocess.Popen(command, creationflags=flags, close_fds=True, **kwargs)
    else:
        subprocess.Popen(command, start_new_session=True, close_fds=True, **kwargs)


def _defer(commands: List[List[str]]) -> bool:
    """Run the removal commands once this process has exited.

    Removing a distribution from under the interpreter that is running it
    leaves locked files on Windows and a half-deleted tree on Unix, so the
    work is handed to a detached child.
    """
    if not commands:
        return False
    try:
        if paths.WINDOWS:
            script = " & ".join(subprocess.list2cmdline(c) for c in commands)
            _spawn(["cmd", "/c", "ping -n %d 127.0.0.1 >nul & %s" % (WAIT_SECONDS + 1, script)])
        else:
            joined = " ; ".join(" ".join(shlex.quote(p) for p in c) for c in commands)
            _spawn(["/bin/sh", "-c", "sleep %d; %s" % (WAIT_SECONDS, joined)])
    except OSError:
        return False
    return True


def schedule(steps: List[Step]) -> bool:
    return _defer([command for _, command in steps])


def run(home: Optional[Path] = None, keep_data: bool = False) -> Tuple[List[Step], Optional[Path], bool]:
    """Plan, delete local data and schedule package removal.

    Returns ``(steps, removed_data_dir, scheduled)``.
    """
    steps = plan(home)
    removed: Optional[Path] = None
    if not keep_data:
        removed = remove_data()
    return steps, removed, schedule(steps)