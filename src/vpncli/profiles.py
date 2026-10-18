from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import paths
from .ovpn import OvpnInfo, parse

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CONFIG_NAME = "config.ovpn"
AUTH_NAME = "auth.txt"


class ProfileError(Exception):
    pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return slug[:64] or "profile"


def _secure(path: Path) -> None:
    if not paths.WINDOWS:
        try:
            os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    paths.give_to_invoker(path)


@dataclass(frozen=True)
class Profile:
    name: str
    directory: Path

    @property
    def config_path(self) -> Path:
        return self.directory / CONFIG_NAME

    @property
    def auth_path(self) -> Path:
        return self.directory / AUTH_NAME

    @property
    def has_credentials(self) -> bool:
        return self.auth_path.is_file()

    def info(self) -> OvpnInfo:
        text = self.config_path.read_text(encoding="utf-8", errors="replace")
        return parse(text)

    def username(self) -> Optional[str]:
        try:
            return self.auth_path.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return None


class ProfileStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root if root is not None else paths.profiles_dir()

    def names(self) -> List[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if (entry / CONFIG_NAME).is_file()
        )

    def all(self) -> List[Profile]:
        return [Profile(name, self.root / name) for name in self.names()]

    def get(self, name: str) -> Profile:
        names = self.names()
        if name in names:
            return Profile(name, self.root / name)
        lowered = name.lower()
        exact = [n for n in names if n.lower() == lowered]
        if len(exact) == 1:
            return Profile(exact[0], self.root / exact[0])
        prefixed = [n for n in names if n.lower().startswith(lowered)]
        if len(prefixed) == 1:
            return Profile(prefixed[0], self.root / prefixed[0])
        if len(prefixed) > 1:
            raise ProfileError(
                "'%s' is ambiguous: %s" % (name, ", ".join(prefixed))
            )
        raise ProfileError("no such profile: %s" % name)

    def add(
        self,
        name: str,
        source: Path,
        username: Optional[str] = None,
        password: Optional[str] = None,
        overwrite: bool = False,
    ) -> Tuple[Profile, List[str]]:
        if not NAME_RE.match(name):
            raise ProfileError(
                "invalid profile name '%s' (letters, digits, '.', '_' and '-' only)"
                % name
            )
        source = Path(source).expanduser()
        if not source.is_file():
            raise ProfileError("file not found: %s" % source)
        text = source.read_text(encoding="utf-8-sig", errors="replace")
        info = parse(text)
        if not info.remotes:
            raise ProfileError(
                "%s has no 'remote' entry and is not a client profile" % source.name
            )

        target = self.root / name
        preserved_auth: Optional[str] = None
        if target.exists():
            if not overwrite:
                raise ProfileError(
                    "profile '%s' already exists (use --force to replace it)" % name
                )
            try:
                preserved_auth = (target / AUTH_NAME).read_text(encoding="utf-8")
            except OSError:
                preserved_auth = None
            shutil.rmtree(str(target))

        paths.ensure_data_dirs()
        target.mkdir(parents=True)
        _secure(target)
        config = target / CONFIG_NAME
        with open(str(config), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        _secure(config)

        warnings = self._copy_assets(source.parent, target, info)
        if info.scripts:
            warnings.append(
                "config runs external commands (%s); review it before connecting"
                % ", ".join(info.scripts)
            )

        if username is not None and password is not None:
            self.set_credentials(name, username, password)
        elif preserved_auth is not None:
            paths.write_private(target / AUTH_NAME, preserved_auth)
        return Profile(name, target), warnings

    def _copy_assets(self, source_dir: Path, target: Path, info: OvpnInfo) -> List[str]:
        warnings: List[str] = []
        references = list(info.assets)
        if info.auth_file:
            references.append(info.auth_file)
        for reference in references:
            relative = Path(reference)
            if relative.is_absolute():
                continue
            if ".." in relative.parts:
                warnings.append(
                    "skipped '%s': path points outside the profile folder" % reference
                )
                continue
            origin = source_dir / relative
            if not origin.is_file():
                warnings.append("referenced file not found: %s" % reference)
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(origin), str(destination))
            _secure(destination)
        return warnings

    def remove(self, name: str) -> None:
        profile = self.get(name)
        shutil.rmtree(str(profile.directory))

    def rename(self, old: str, new: str) -> Profile:
        profile = self.get(old)
        if not NAME_RE.match(new):
            raise ProfileError("invalid profile name '%s'" % new)
        target = self.root / new
        if target.exists():
            raise ProfileError("profile '%s' already exists" % new)
        profile.directory.rename(target)
        return Profile(new, target)

    def set_credentials(self, name: str, username: str, password: str) -> None:
        if not username or not password:
            raise ProfileError("username and password must not be empty")
        if "\n" in username or "\r" in username or "\n" in password or "\r" in password:
            raise ProfileError("credentials must not contain line breaks")
        profile = self.get(name)
        paths.write_private(profile.auth_path, "%s\n%s\n" % (username, password))

    def clear_credentials(self, name: str) -> bool:
        profile = self.get(name)
        if not profile.has_credentials:
            return False
        paths.remove_quietly(profile.auth_path)
        return True
