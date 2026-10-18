from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

FILE_DIRECTIVES = frozenset(
    {
        "ca",
        "cert",
        "key",
        "dh",
        "tls-auth",
        "tls-crypt",
        "tls-crypt-v2",
        "pkcs12",
        "crl-verify",
        "extra-certs",
        "secret",
        "askpass",
    }
)

SCRIPT_DIRECTIVES = frozenset(
    {
        "up",
        "down",
        "route-up",
        "route-pre-down",
        "ipchange",
        "tls-verify",
        "auth-user-pass-verify",
        "client-connect",
        "client-disconnect",
        "learn-address",
        "plugin",
    }
)

DEFAULT_PORT = 1194
DEFAULT_PROTO = "udp"


@dataclass(frozen=True)
class Remote:
    host: str
    port: int
    proto: str

    @property
    def is_tcp(self) -> bool:
        return self.proto.startswith("tcp")

    def __str__(self) -> str:
        return "%s:%d/%s" % (self.host, self.port, self.proto)


@dataclass
class OvpnInfo:
    remotes: List[Remote] = field(default_factory=list)
    assets: List[str] = field(default_factory=list)
    scripts: List[str] = field(default_factory=list)
    inline: List[str] = field(default_factory=list)
    auth_file: Optional[str] = None
    needs_auth: bool = False


def _tokens(line: str) -> List[str]:
    if "\\" in line:
        parts = line.split()
    else:
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
    cleaned = []
    for part in parts:
        if part[:1] in ("#", ";"):
            break
        cleaned.append(part.strip("\"'"))
    return cleaned


def _port(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _append_unique(items: List[str], value: str) -> None:
    if value not in items:
        items.append(value)


def parse(text: str) -> OvpnInfo:
    info = OvpnInfo()
    default_proto = DEFAULT_PROTO
    default_port = DEFAULT_PORT
    raw_remotes: List[Tuple[str, Optional[str], Optional[str]]] = []
    wants_auth = False
    open_tag: Optional[str] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if open_tag is not None:
            if line == "</%s>" % open_tag:
                open_tag = None
            continue
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1].strip()
            if tag.startswith("/") or tag == "connection":
                continue
            open_tag = tag
            _append_unique(info.inline, tag)
            continue
        tokens = _tokens(line)
        if not tokens:
            continue
        key = tokens[0].lower()
        args = tokens[1:]
        if key == "remote" and args:
            port = args[1] if len(args) > 1 else None
            proto = args[2] if len(args) > 2 else None
            raw_remotes.append((args[0], port, proto))
        elif key == "proto" and args:
            default_proto = args[0].lower()
        elif key in ("port", "rport") and args:
            default_port = _port(args[0], default_port)
        elif key == "auth-user-pass":
            if args:
                info.auth_file = args[0]
            else:
                wants_auth = True
        elif key in FILE_DIRECTIVES and args:
            if not args[0].startswith("["):
                _append_unique(info.assets, args[0])
        elif key in SCRIPT_DIRECTIVES:
            _append_unique(info.scripts, key)

    for host, port, proto in raw_remotes:
        info.remotes.append(
            Remote(host, _port(port, default_port), (proto or default_proto).lower())
        )
    info.needs_auth = (
        wants_auth
        and info.auth_file is None
        and "auth-user-pass" not in info.inline
    )
    return info
