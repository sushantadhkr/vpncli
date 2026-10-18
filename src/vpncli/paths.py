from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

APP_NAME = "vpncli"
WINDOWS = sys.platform.startswith("win")


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def invoking_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and is_root():
        try:
            import pwd

            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (ImportError, KeyError):
            pass
    return Path.home()


def data_dir() -> Path:
    override = os.environ.get("VPNCLI_HOME")
    if override:
        return Path(override).expanduser()
    home = invoking_home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if WINDOWS:
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and not os.environ.get("SUDO_USER"):
        return Path(xdg) / APP_NAME
    return home / ".config" / APP_NAME


def profiles_dir() -> Path:
    return data_dir() / "profiles"


def run_dir() -> Path:
    return data_dir() / "run"


def state_file() -> Path:
    return run_dir() / "state.json"


def log_file() -> Path:
    return run_dir() / "openvpn.log"


def pid_file() -> Path:
    return run_dir() / "openvpn.pid"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def invoker_ids() -> Optional[Tuple[int, int]]:
    uid = os.environ.get("VPNCLI_UID") or os.environ.get("SUDO_UID")
    gid = os.environ.get("VPNCLI_GID") or os.environ.get("SUDO_GID")
    if not uid:
        return None
    try:
        return int(uid), int(gid or uid)
    except ValueError:
        return None


def give_to_invoker(path: Path) -> None:
    if not is_root():
        return
    ids = invoker_ids()
    if ids is None:
        return
    try:
        os.chown(str(path), ids[0], ids[1])
    except OSError:
        pass


def ensure_data_dirs() -> None:
    for directory in (data_dir(), profiles_dir(), run_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        if not WINDOWS:
            try:
                os.chmod(str(directory), 0o700)
            except OSError:
                pass
        give_to_invoker(directory)


def write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(str(tmp), str(path))
    if not WINDOWS:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    give_to_invoker(path)


def ensure_shared(path: Path) -> None:
    """Pre-create a file that openvpn will later open for writing.

    When openvpn runs elevated it does so with sudo's umask (0077), so any
    file it creates itself becomes 0600 root-owned and can never be read
    back by this process. Creating the file first means openvpn reuses the
    existing file and inherits our mode and owner. Root can write to it
    regardless of mode, so this works with or without elevation, and it
    never loosens permissions beyond what the caller asked for.
    """
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    os.close(fd)
    give_to_invoker(path)
    if not WINDOWS:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass


def remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
