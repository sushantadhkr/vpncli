from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
import urllib.request
from typing import List, Optional

from .ovpn import OvpnInfo, Remote

WINDOWS = sys.platform.startswith("win")
IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)
PING_TIME = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def public_ip(timeout: float = 6.0) -> Optional[str]:
    for url in IP_SERVICES:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "vpncli"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = response.read(128).decode("ascii", "ignore").strip()
        except (OSError, ValueError):
            continue
        if IPV4.match(value) or ":" in value:
            return value
    return None


def tcp_latency(host: str, port: int, timeout: float = 3.0) -> Optional[float]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return (time.perf_counter() - started) * 1000.0


def ping_latency(host: str, timeout: float = 3.0) -> Optional[float]:
    if WINDOWS:
        command = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        command = ["ping", "-c", "1", host]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=timeout + 2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = PING_TIME.search(result.stdout or "")
    if result.returncode != 0 or not match:
        return None
    return float(match.group(1))


def remote_latency(remote: Remote) -> Optional[float]:
    if remote.is_tcp:
        return tcp_latency(remote.host, remote.port)
    return ping_latency(remote.host)


def profile_latency(info: OvpnInfo, limit: int = 3) -> Optional[float]:
    samples: List[float] = []
    for remote in info.remotes[:limit]:
        value = remote_latency(remote)
        if value is not None:
            samples.append(value)
    return min(samples) if samples else None
