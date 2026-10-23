from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from . import paths

WINDOWS = sys.platform.startswith("win")


def is_admin() -> bool:
    if WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return paths.is_root()


def _environment() -> Dict[str, str]:
    env = {"VPNCLI_HOME": str(paths.data_dir())}
    if hasattr(os, "getuid"):
        env["VPNCLI_UID"] = str(os.getuid())
        env["VPNCLI_GID"] = str(os.getgid())
    from . import runtime

    try:
        env["VPNCLI_OPENVPN"] = runtime.find_openvpn()
    except runtime.VpnError:
        pass
    return env


def _applescript_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _helper_socket_path() -> Optional[str]:
    for candidate in ("/var/run/vpncli-helper.path", os.path.expanduser("~/.vpncli-helper.path")):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                path = handle.read().strip()
            if path and os.path.exists(path):
                return path
        except OSError:
            continue
    for candidate in ("/var/run/vpncli-helper.sock", "/tmp/vpncli-helper.sock"):
        if os.path.exists(candidate):
            return candidate
    return None


def _run_via_helper(args: List[str]) -> Optional[Tuple[int, str]]:
    path = _helper_socket_path()
    if path is None:
        return None
    if sys.platform == "win32":
        return None  # named pipes not implemented yet
    import socket as _socket

    env = _environment()
    command = [sys.executable, "-m", "vpncli"] + list(args)
    req = {"cmd": "run", "argv": command, "env": env}
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(300)
        sock.connect(path)
        sock.sendall((json.dumps(req) + "\\n").encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
    except (OSError, socket.error):
        return None
    try:
        resp = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    if not resp.get("ok"):
        return 1, resp.get("error", "helper error")
    return int(resp.get("code", 1)), resp.get("stdout", "")


def run_elevated(args: List[str]) -> Tuple[int, str]:
    # Prefer the persistent helper when available (no password prompt).
    helper_result = _run_via_helper(args)
    if helper_result is not None:
        return helper_result

    command = [sys.executable, "-m", "vpncli"] + list(args)
    env = _environment()

    if is_admin():
        merged = dict(os.environ)
        merged.update(env)
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=merged,
        )
        return result.returncode, result.stdout or ""

    if sys.platform == "darwin":
        # Prefer passwordless sudo when available (one-time sudoers rule),
        # so connects don't prompt. Fall back to the osascript dialog.
        try:
            probe = subprocess.run(
                ["sudo", "-n", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                assignments = ["%s=%s" % item for item in env.items()]
                result = subprocess.run(
                    ["sudo", "-n", "env"] + assignments + command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                )
                return result.returncode, result.stdout or ""
        except OSError:
            pass
        assignments = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in env.items())
        inner = "env %s %s" % (assignments, " ".join(shlex.quote(c) for c in command))
        script = 'do shell script "%s" with administrator privileges' % _applescript_quote(inner)
        result = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        return result.returncode, result.stdout or ""

    if WINDOWS:
        import ctypes

        params = subprocess.list2cmdline(command[1:])
        outcome = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", command[0], params, None, 0
        )
        if outcome > 32:
            return 0, ""
        return 1, "administrator permission was not granted"

    pkexec = shutil.which("pkexec")
    if pkexec is None:
        return 1, "pkexec (polkit) is required to ask for administrator permission"
    assignments = ["%s=%s" % item for item in env.items()]
    result = subprocess.run(
        [pkexec, "env"] + assignments + command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    return result.returncode, result.stdout or ""
