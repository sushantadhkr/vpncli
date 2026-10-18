from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import paths, privilege
from .profiles import Profile

WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000

INSTALL_HINT = {
    "darwin": "install it with: brew install openvpn",
    "linux": "install it with your package manager, e.g. sudo apt install openvpn",
    "win32": "install it with: winget install OpenVPNTechnologies.OpenVPN",
}

KNOWN_LOCATIONS = {
    "darwin": (
        "/opt/homebrew/sbin/openvpn",
        "/opt/homebrew/bin/openvpn",
        "/usr/local/sbin/openvpn",
        "/usr/local/bin/openvpn",
    ),
    "linux": (
        "/usr/sbin/openvpn",
        "/usr/local/sbin/openvpn",
        "/sbin/openvpn",
        "/usr/bin/openvpn",
    ),
    "win32": (
        r"C:\Program Files\OpenVPN\bin\openvpn.exe",
        r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
    ),
}

STATE_MARKERS = (
    ("Initialization Sequence Completed", "connected"),
    ("SIGTERM[soft,auth-failure]", "auth_failed"),
    ("AUTH_FAILED", "auth_failed"),
    ("Exiting due to fatal error", "error"),
    ("SIGTERM[hard,] received", "disconnected"),
    ("SIGTERM[soft,", "disconnected"),
    ("SIGUSR1[soft,", "reconnecting"),
    ("SIGUSR1[hard,", "reconnecting"),
    ("Restart pause", "reconnecting"),
)
ERROR_HINTS = (
    "AUTH_FAILED",
    "fatal error",
    "TLS Error",
    "TLS handshake failed",
    "Options error",
    "Cannot resolve",
    "Cannot open",
    "Connection refused",
)
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+")
SERVER_PATTERN = re.compile(r"Peer Connection Initiated with \[AF_INET6?\](\S+):(\d+)\s*$")
DEVICE_PATTERNS = (
    re.compile(r"TUN/TAP device (\S+) opened"),
    re.compile(r"Opened tun device (\S+)"),
)
LOCAL_PATTERNS = (
    re.compile(r"net_addr_v4_add: (?P<ip>\d+\.\d+\.\d+\.\d+)/\d+ dev (?P<dev>\S+)"),
    re.compile(r"ip(?:\.exe)? addr add dev (?P<dev>\S+) (?:local )?(?P<ip>\d+\.\d+\.\d+\.\d+)"),
    re.compile(r"ifconfig (?P<dev>\S+) (?P<ip>\d+\.\d+\.\d+\.\d+) "),
    re.compile(r"DHCP IP/netmask of (?P<ip>\d+\.\d+\.\d+\.\d+)"),
)
MAX_LOG_BYTES = 4 * 1024 * 1024


class VpnError(Exception):
    pass


@dataclass
class Session:
    profile: str
    pid: int
    started: float


@dataclass
class LinkStatus:
    state: str = "connecting"
    detail: str = ""
    server: Optional[str] = None
    local_ip: Optional[str] = None
    device: Optional[str] = None


@dataclass
class Status:
    state: str = "disconnected"
    profile: Optional[str] = None
    pid: Optional[int] = None
    since: Optional[float] = None
    server: Optional[str] = None
    local_ip: Optional[str] = None
    device: Optional[str] = None
    detail: str = ""

    @property
    def uptime(self) -> Optional[float]:
        if self.since is None or self.state != "connected":
            return None
        return max(0.0, time.time() - self.since)

    def as_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["uptime"] = self.uptime
        return data


def find_openvpn() -> str:
    override = os.environ.get("VPNCLI_OPENVPN")
    if override and os.path.isfile(override):
        return override
    found = shutil.which("openvpn")
    if found:
        return found
    for candidate in KNOWN_LOCATIONS.get(sys.platform if sys.platform in KNOWN_LOCATIONS else "linux", ()):
        if os.path.isfile(candidate):
            return candidate
    platform_key = "win32" if WINDOWS else sys.platform
    hint = INSTALL_HINT.get(platform_key, INSTALL_HINT["linux"])
    raise VpnError("openvpn was not found; " + hint)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                creationflags=NO_WINDOW,
            )
        except OSError:
            return False
        return '"%d"' % pid in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_session() -> Optional[Session]:
    try:
        data = json.loads(paths.state_file().read_text(encoding="utf-8"))
        return Session(str(data["profile"]), int(data["pid"]), float(data["started"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_session(session: Session) -> None:
    paths.write_private(paths.state_file(), json.dumps(asdict(session)))


def clear_session() -> None:
    paths.remove_quietly(paths.state_file())


def active_session() -> Optional[Session]:
    session = load_session()
    if session is None:
        return None
    if pid_alive(session.pid):
        return session
    clear_session()
    return None


def read_log() -> str:
    path = paths.log_file()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_LOG_BYTES:
                handle.seek(size - MAX_LOG_BYTES)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def tail_log(lines: int = 50) -> List[str]:
    content = read_log().splitlines()
    return content[-lines:] if lines > 0 else content


def follow_log(poll: float = 0.5) -> Iterator[str]:
    path = paths.log_file()
    try:
        position = path.stat().st_size
    except OSError:
        position = 0
    buffer = ""
    while True:
        try:
            size = path.stat().st_size
            if size < position:
                position = 0
                buffer = ""
            if size > position:
                with path.open("rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position += len(chunk)
                buffer += chunk.decode("utf-8", "replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    yield line.rstrip("\r")
        except OSError:
            pass
        time.sleep(poll)


def parse_log(text: str) -> LinkStatus:
    link = LinkStatus()
    last_error = ""
    for raw in text.splitlines():
        line = TIMESTAMP.sub("", raw.strip())
        for marker, state in STATE_MARKERS:
            if marker in line:
                link.state = state
                break
        if any(hint in line for hint in ERROR_HINTS):
            last_error = line
        server = SERVER_PATTERN.search(line)
        if server:
            link.server = server.group(1)
        for pattern in DEVICE_PATTERNS:
            device = pattern.search(line)
            if device:
                link.device = device.group(1)
        for pattern in LOCAL_PATTERNS:
            local = pattern.search(line)
            if local:
                link.local_ip = local.group("ip")
                if "dev" in pattern.groupindex:
                    link.device = local.group("dev")
    if link.state in ("auth_failed", "error", "reconnecting"):
        link.detail = last_error
    return link


def get_status() -> Status:
    session = active_session()
    if session is None:
        return Status()
    link = parse_log(read_log())
    state = "disconnecting" if link.state == "disconnected" else link.state
    return Status(
        state=state,
        profile=session.profile,
        pid=session.pid,
        since=session.started,
        server=link.server,
        local_ip=link.local_ip,
        device=link.device,
        detail=link.detail,
    )


def build_command(
    openvpn: str,
    profile: Profile,
    auth_path: Optional[Path],
    logfile: Optional[Path],
    pidfile: Optional[Path],
    daemon: bool,
    extra: Sequence[str] = (),
) -> List[str]:
    command = [openvpn, "--cd", str(profile.directory), "--config", str(profile.config_path)]
    if auth_path is not None:
        command += ["--auth-user-pass", str(auth_path)]
    command += ["--verb", "3"]
    if pidfile is not None:
        command += ["--writepid", str(pidfile)]
    if logfile is not None:
        command += ["--log", str(logfile)]
    if daemon:
        command.append("--daemon")
    command += list(extra)
    return command


def _privileged(command: List[str], interactive: bool) -> List[str]:
    if WINDOWS or paths.is_root():
        return command
    sudo = shutil.which("sudo")
    if sudo is None:
        raise VpnError("sudo is required to run openvpn; install sudo or run as root")
    prefix = [sudo] if interactive else [sudo, "-n"]
    return prefix + command


def _require_privileges() -> None:
    if WINDOWS and not privilege.is_admin():
        raise VpnError("administrator rights are required; open an elevated terminal")


def _resolve_auth(
    profile: Profile, credentials: Optional[Tuple[str, str]]
) -> Tuple[Optional[Path], Optional[Path]]:
    if credentials is not None:
        temporary = paths.run_dir() / "auth.tmp"
        paths.write_private(temporary, "%s\n%s\n" % credentials)
        return temporary, temporary
    if profile.has_credentials:
        return profile.auth_path, None
    if profile.info().needs_auth:
        raise VpnError(
            "profile '%s' needs a username and password; run: vpncli creds %s"
            % (profile.name, profile.name)
        )
    return None, None


def _read_pid() -> Optional[int]:
    try:
        return int(paths.pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _launch(command: List[str]) -> int:
    if WINDOWS:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | NO_WINDOW
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        return process.pid
    result = subprocess.run(command)
    if result.returncode != 0:
        detail = parse_log(read_log()).detail or " | ".join(tail_log(3))
        raise VpnError("openvpn failed to start" + (": " + detail if detail else ""))
    deadline = time.time() + 8.0
    while time.time() < deadline:
        pid = _read_pid()
        if pid:
            return pid
        time.sleep(0.2)
    raise VpnError("openvpn started but did not report a process id")


def _await_connection(
    session: Session,
    timeout: float,
    progress: Optional[Callable[[str], None]],
) -> None:
    deadline = time.time() + timeout
    announced: Optional[str] = None
    while time.time() < deadline:
        link = parse_log(read_log())
        if link.state == "connected":
            return
        if link.state in ("auth_failed", "error"):
            raise VpnError(link.detail or "connection failed; see: vpncli logs")
        if not pid_alive(session.pid):
            raise VpnError(
                "openvpn exited unexpectedly: "
                + (link.detail or " | ".join(tail_log(3)) or "no output")
            )
        if progress is not None and link.state != announced:
            announced = link.state
            progress(link.state)
        time.sleep(0.5)
    raise VpnError("timed out after %ds; see: vpncli logs" % int(timeout))


def connect(
    profile: Profile,
    credentials: Optional[Tuple[str, str]] = None,
    timeout: float = 45.0,
    interactive: bool = True,
    extra: Sequence[str] = (),
    progress: Optional[Callable[[str], None]] = None,
) -> Session:
    openvpn = find_openvpn()
    _require_privileges()
    paths.ensure_data_dirs()
    current = active_session()
    if current is not None:
        raise VpnError("already connected to '%s'" % current.profile)

    auth_path, temporary = _resolve_auth(profile, credentials)
    paths.remove_quietly(paths.log_file())
    paths.remove_quietly(paths.pid_file())
    paths.ensure_shared(paths.log_file())
    paths.ensure_shared(paths.pid_file())
    command = build_command(
        openvpn, profile, auth_path, paths.log_file(), paths.pid_file(), True, extra
    )
    try:
        pid = _launch(_privileged(command, interactive))
        session = Session(profile.name, pid, time.time())
        save_session(session)
        try:
            _await_connection(session, timeout, progress)
        except VpnError:
            disconnect(interactive=interactive)
            raise
    finally:
        if temporary is not None:
            paths.remove_quietly(temporary)
    return session


def run_foreground(
    profile: Profile,
    credentials: Optional[Tuple[str, str]] = None,
    interactive: bool = True,
    extra: Sequence[str] = (),
) -> int:
    openvpn = find_openvpn()
    _require_privileges()
    paths.ensure_data_dirs()
    if active_session() is not None:
        raise VpnError("a background connection is active; run: vpncli disconnect")
    auth_path, temporary = _resolve_auth(profile, credentials)
    command = build_command(openvpn, profile, auth_path, None, None, False, extra)
    try:
        return subprocess.call(_privileged(command, interactive))
    except KeyboardInterrupt:
        return 130
    finally:
        if temporary is not None:
            paths.remove_quietly(temporary)


def _terminate(pid: int, force: bool, interactive: bool) -> None:
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        return
    chosen = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, chosen)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    sudo = shutil.which("sudo")
    if sudo is None:
        raise VpnError("sudo is required to stop openvpn")
    prefix = [sudo] if interactive else [sudo, "-n"]
    subprocess.run(prefix + ["kill", "-%d" % int(chosen), str(pid)])


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.25)
    return not pid_alive(pid)


def disconnect(timeout: float = 12.0, interactive: bool = True) -> Optional[str]:
    session = load_session()
    if session is None:
        return None
    if pid_alive(session.pid):
        _terminate(session.pid, False, interactive)
        if not _wait_gone(session.pid, timeout):
            _terminate(session.pid, True, interactive)
            _wait_gone(session.pid, 5.0)
    if pid_alive(session.pid):
        raise VpnError("could not stop openvpn (pid %d)" % session.pid)
    clear_session()
    paths.remove_quietly(paths.pid_file())
    return session.profile
