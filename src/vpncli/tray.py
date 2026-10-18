from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Iterator, Optional

from . import assets, netutil, paths, privilege, runtime
from .profiles import ProfileStore

COLORS = {
    "connected": (46, 204, 113, 255),
    "connecting": (245, 166, 35, 255),
    "reconnecting": (245, 166, 35, 255),
    "disconnecting": (245, 166, 35, 255),
    "auth_failed": (231, 76, 60, 255),
    "error": (231, 76, 60, 255),
    "disconnected": (138, 143, 152, 255),
}
BUSY_STATES = ("connecting", "reconnecting", "disconnecting")
POLL_SECONDS = 2.0


def build_icon(state: str):
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = COLORS.get(state, COLORS["disconnected"])
    draw.polygon([(32, 3), (57, 12), (57, 33), (32, 61), (7, 33), (7, 12)], fill=color)
    white = (255, 255, 255, 255)
    if state == "connected":
        draw.line([(20, 32), (29, 42), (45, 22)], fill=white, width=6, joint="curve")
    else:
        draw.line([(21, 22), (32, 42), (43, 22)], fill=white, width=6, joint="curve")
    return image


def open_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def dependencies_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("pystray", "PIL"))


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no details available"


class TrayApp:
    def __init__(self) -> None:
        import pystray

        self.pystray = pystray
        self.store = ProfileStore()
        self.status = runtime.get_status()
        self.busy = False
        self.stop_event = threading.Event()
        self.icons: Dict[str, object] = {}
        self.icon = pystray.Icon(
            "vpncli",
            self.icon_for(self.status.state),
            self.title(),
            menu=pystray.Menu(self.menu_items),
        )

    def icon_for(self, state: str):
        if state not in self.icons:
            self.icons[state] = build_icon(state)
        return self.icons[state]

    def title(self) -> str:
        status = self.status
        if status.state == "connected":
            return "vpncli: connected to %s" % status.profile
        if status.state == "disconnected":
            return "vpncli: disconnected"
        label = status.state.replace("_", " ")
        return "vpncli: %s%s" % (label, " (%s)" % status.profile if status.profile else "")

    def menu_items(self) -> Iterator[object]:
        pystray = self.pystray
        item = pystray.MenuItem
        yield item(lambda entry: self.title(), None, enabled=False)
        if self.status.local_ip:
            yield item(lambda entry: "IP: %s" % (self.status.local_ip or "-"), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield item(
            "Connect",
            pystray.Menu(self.profile_items),
            enabled=lambda entry: not self.busy and bool(self.store.names()),
        )
        yield item(
            "Disconnect",
            self.on_disconnect,
            enabled=lambda entry: not self.busy and self.status.state != "disconnected",
        )
        yield item(
            "Reconnect",
            self.on_reconnect,
            enabled=lambda entry: not self.busy and self.status.state != "disconnected",
        )
        yield pystray.Menu.SEPARATOR
        yield item("Show public IP", self.on_show_ip)
        yield item("Open log", self.on_open_log)
        yield item("Open profiles folder", self.on_open_profiles)
        yield pystray.Menu.SEPARATOR
        yield pystray.Menu.SEPARATOR
        yield item(
            "Uninstall vpncli...",
            pystray.Menu(self.uninstall_items),
            enabled=lambda entry: not self.busy,
        )
        yield item("Quit", self.on_quit)

    def uninstall_items(self) -> Iterator[object]:
        pystray = self.pystray
        item = pystray.MenuItem
        yield item(
            lambda entry: "Removes the tray icon, the vpncli command and %s"
            % paths.data_dir(),
            None,
            enabled=False,
        )
        yield pystray.Menu.SEPARATOR
        yield item("Uninstall and delete all data", self.on_uninstall(True))
        yield item("Uninstall but keep profiles", self.on_uninstall(False))

    def on_uninstall(self, purge: bool) -> None:
        def action() -> None:
            args = ["uninstall", "--yes"] + ([] if purge else ["--keep-data"])
            self.busy = True
            self.icon.update_menu()
            try:
                code, output = privilege.run_elevated(args)
                if code != 0:
                    print("uninstall failed: %s" % last_line(output), file=sys.stderr)
                else:
                    print(output)
            finally:
                # Leave the icon up until the removal has actually been asked
                # for, otherwise the tray would exit before the prompt appears.
                self.busy = False
                self.stop_event.set()
                try:
                    self.icon.stop()
                except Exception:
                    pass

        threading.Thread(target=action, daemon=True).start()

    def profile_items(self) -> Iterator[object]:
        names = self.store.names()
        if not names:
            yield self.pystray.MenuItem("No profiles (vpncli add ...)", None, enabled=False)
        for name in names:
            yield self.pystray.MenuItem(
                name,
                self.connect_action(name),
                checked=self.checked_for(name),
                radio=True,
            )

    def connect_action(self, name: str):
        def action() -> None:
            self.run_task("Connect", ["connect", name, "--non-interactive"])

        return action

    def checked_for(self, name: str):
        def checked(entry) -> bool:
            return self.status.profile == name and self.status.state != "disconnected"

        return checked

    def on_disconnect(self) -> None:
        self.run_task("Disconnect", ["disconnect", "--non-interactive"])

    def on_reconnect(self) -> None:
        self.run_task("Reconnect", ["reconnect", "--non-interactive"])

    def on_show_ip(self) -> None:
        def worker() -> None:
            address = netutil.public_ip()
            self.notify("Public IP", address or "could not be determined")

        threading.Thread(target=worker, daemon=True).start()

    def on_open_log(self) -> None:
        if paths.log_file().exists():
            open_path(paths.log_file())
        else:
            self.notify("vpncli", "No log yet; connect first")

    def on_open_profiles(self) -> None:
        paths.ensure_data_dirs()
        open_path(paths.profiles_dir())

    def on_quit(self) -> None:
        self.stop_event.set()
        self.icon.stop()

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def run_task(self, label: str, args) -> None:
        if self.busy:
            return

        def worker() -> None:
            self.busy = True
            self.icon.update_menu()
            try:
                code, output = privilege.run_elevated(list(args))
                if code != 0:
                    self.notify("%s failed" % label, last_line(output))
            finally:
                self.busy = False
                self.refresh()

        threading.Thread(target=worker, daemon=True).start()

    def refresh(self) -> None:
        try:
            status = runtime.get_status()
        except Exception:
            return
        changed = status.state != self.status.state or status.profile != self.status.profile
        self.status = status
        self.icon.icon = self.icon_for(status.state)
        self.icon.title = self.title()
        if changed:
            self.icon.update_menu()

    def poll(self) -> None:
        while not self.stop_event.is_set():
            self.refresh()
            self.stop_event.wait(POLL_SECONDS)

    def setup(self, icon) -> None:
        icon.visible = True
        threading.Thread(target=self.poll, daemon=True).start()

    def run(self) -> None:
        self.icon.run(self.setup)


def main() -> int:
    if not dependencies_available():
        print(
            "The tray needs extra packages. Install them with:\n"
            "  pipx install 'vpncli[gui] @ git+https://github.com/sushantadhkr/vpncli.git'\n"
            "or, inside an existing environment:\n"
            "  pip install 'vpncli[gui]'",
            file=sys.stderr,
        )
        return 1
    try:
        app = TrayApp()
    except Exception as exc:
        print("could not start the tray icon: %s" % exc, file=sys.stderr)
        return 1
    # First run: put the icon and the menu bar entry in place, quietly.
    try:
        assets.register()
    except Exception:
        pass
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
