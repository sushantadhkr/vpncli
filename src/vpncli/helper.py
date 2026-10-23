"""Privileged helper daemon for vpncli.

A small, long-running process that runs as root/admin and exposes a narrow
API over a local socket so the (unprivileged) CLI and tray can perform
privileged operations (running openvpn, killing it) without prompting for a
password every time. Mirrors the Tunnelblick privileged-helper model.

Protocol: newline-delimited JSON over a Unix domain socket (macOS/Linux) or a
named pipe (Windows). Each request is one JSON object; the response is one JSON
object.

Commands:
  ping  -> {"ok": true}
  run   -> {"argv": [...], "env": {...}}  runs argv as root, returns
           {"code": int, "stdout": str}
  kill  -> {"pid": int}  sends SIGTERM, returns {"ok": bool}
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from typing import Any, Dict

SOCKET_PATH = "/var/run/vpncli-helper.sock"
ALT_SOCKET_PATH = "/tmp/vpncli-helper.sock"


def socket_path() -> str:
    # Prefer /var/run if writable (root), else /tmp.
    if os.path.isdir("/var/run") and os.access("/var/run", os.W_OK):
        return SOCKET_PATH
    return ALT_SOCKET_PATH


def _allowed_argv(argv) -> bool:
    """Minimal guard: only allow running python/vpncli or openvpn."""
    if not isinstance(argv, list) or not argv:
        return False
    base = os.path.basename(str(argv[0]))
    if not (base == "openvpn" or base.startswith("python")):
        return False
    joined = " ".join(str(a) for a in argv)
    if "vpncli" not in joined and "openvpn" not in joined:
        return False
    return True


def handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True}
    if cmd == "run":
        argv = req.get("argv")
        if not _allowed_argv(argv):
            return {"ok": False, "error": "disallowed command"}
        env = dict(os.environ)
        env.update(req.get("env") or {})
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                env=env,
            )
            return {"ok": True, "code": result.returncode, "stdout": result.stdout or ""}
        except Exception as exc:  # pylint: disable=broad-except
            return {"ok": False, "error": str(exc)}
    if cmd == "kill":
        pid = req.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return {"ok": False, "error": "bad pid"}
        try:
            os.kill(pid, signal.SIGTERM)
            return {"ok": True}
        except ProcessLookupError:
            return {"ok": True}
        except Exception as exc:  # pylint: disable=broad-except
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "unknown command: %s" % cmd}


def _serve_unix(path: str) -> None:
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    sock.listen(16)
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            continue
        threading.Thread(target=_handle_conn, args=(conn,), daemon=True).start()


def _handle_conn(conn) -> None:
    data = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
    except OSError:
        pass
    resp = {"ok": False, "error": "bad request"}
    if data:
        try:
            req = json.loads(data.decode("utf-8", "replace"))
            resp = handle_request(req)
        except (ValueError, TypeError) as exc:
            resp = {"ok": False, "error": "invalid json: %s" % exc}
    try:
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def daemonize() -> None:
    """Classic double-fork daemonize (Unix)."""
    if sys.platform == "win32":
        return
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    # redirect stdio
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    os.open("/dev/null", os.O_RDONLY)
    os.open("/dev/null", os.O_WRONLY)
    os.open("/dev/null", os.O_WRONLY)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vpncli-helper", description="vpncli privileged helper daemon")
    parser.add_argument("--foreground", action="store_true", help="do not daemonize")
    parser.add_argument("--socket", default=None, help="socket path (default: auto)")
    args = parser.parse_args(argv)

    path = args.socket or socket_path()
    # Write the chosen socket path so clients can find it.
    try:
        with open("/var/run/vpncli-helper.path", "w") as f:
            f.write(path + "\n")
    except OSError:
        try:
            with open(os.path.expanduser("~/.vpncli-helper.path"), "w") as f:
                f.write(path + "\n")
        except OSError:
            pass

    if not args.foreground and sys.platform != "win32":
        daemonize()

    _serve_unix(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())