from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Iterator, Optional

from . import assets, netutil, paths, privilege, runtime, update
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


def confirm_update() -> bool:
    """Ask before running the installer, using the native dialog of each system."""
    question = "Download and install the latest vpncli?"
    try:
        if sys.platform == "darwin":
            script = 'display dialog "%s" with title "vpncli update" buttons {"Cancel", "Update"} default button "Update"' % question
            result = subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0
        if sys.platform.startswith("win"):
            import ctypes

            answer = ctypes.windll.user32.MessageBoxW(0, question, "vpncli update", 0x04 | 0x20)
            return answer == 6  # IDYES
        zenity = shutil.which("zenity")
        if zenity:
            result = subprocess.run(
                [zenity, "--question", "--title", "vpncli update", question],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return result.returncode == 0
    except Exception:
        pass
    return False


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no details available"


def promote_to_ui_element() -> None:
    """Make this process a macOS menu-bar-only (UIElement) app.

    A plain Python process defaults to the "Regular" activation policy, which
    puts a "Python" icon in the Dock and can prevent the status item from
    rendering in the menu bar. TransformProcessType to a UIElement application
    is the reliable, synchronous, pre-run-loop way to fix that; the AppKit
    setActivationPolicy call is kept as a best-effort backup.
    """
    if sys.platform != "darwin":
        return
    # Carbon TransformProcessType: reliable pre-run-loop promotion.
    try:
        import ctypes
        from ctypes import Structure, c_uint32

        class ProcessSerialNumber(Structure):
            _fields_ = [("highLongOfPSN", c_uint32), ("lowLongOfPSN", c_uint32)]

        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        get_current = carbon.GetCurrentProcess
        get_current.argtypes = [ctypes.POINTER(ProcessSerialNumber)]
        get_current.restype = c_int32
        transform = carbon.TransformProcessType
        transform.argtypes = [ctypes.POINTER(ProcessSerialNumber), c_uint32]
        transform.restype = c_int32
        psn = ProcessSerialNumber()
        if get_current(ctypes.byref(psn)) == 0:
            # kProcessTransformToUIElementApplication == 4
            transform(ctypes.byref(psn), 4)
    except Exception:
        pass
    # AppKit backup (best effort; silently dropped on macOS 13-15 pre-runloop).
    try:
        import AppKit

        AppKit.NSApp.setActivationPolicy_(1)  # Accessory
    except Exception:
        pass


class TrayApp:
    def __init__(self) -> None:
        # macOS: promote to a UIElement (menu-bar-only) app BEFORE creating
        # the status item. Creating the NSStatusItem while the process is still
        # "Regular" leaves it invisible even after promotion (vault-mtg finding).
        if sys.platform == "darwin":
            promote_to_ui_element()
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
        yield item("Check for updates...", self.on_update)
        yield item(
            "Uninstall vpncli...",
            pystray.Menu(self.uninstall_items),
            enabled=lambda entry: not self.busy,
        )
        yield item("Quit", self.on_quit)

    def on_update(self) -> None:
        def worker() -> None:
            try:
                url, _ = update.update(dry_run=True)
                message = "Latest installer: %s" % url
            except Exception as exc:
                message = last_line(str(exc))
            self.notify("vpncli", message)
            if not confirm_update():
                return
            self.busy = True
            self.icon.update_menu()
            try:
                update.update()
                message = "vpncli has been updated"
            except Exception as exc:
                message = last_line(str(exc))
            finally:
                self.busy = False
            self.notify("vpncli", message)
            # The environment was just replaced, so keep the icon open rather
            # than sitting on files that no longer exist.
            self.refresh()

        threading.Thread(target=worker, daemon=True).start()

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
        yield item("Uninstall and delete all data", self.uninstall_action(True))
        yield item("Uninstall but keep profiles", self.uninstall_action(False))

    def uninstall_action(self, purge: bool):
        """Build the callback for one uninstall entry.

        This must return the callback rather than run it: pystray rebuilds
        the menu every time the state changes, so anything with a side
        effect here would fire over and over.
        """
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

        return action

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
        try:
            icon.visible = True
        except Exception as exc:
            print("vpncli tray: could not show icon: %s" % exc, file=sys.stderr)
        threading.Thread(target=self.poll, daemon=True).start()
        if sys.platform == "darwin":
            promote_to_ui_element()
            # Deferred AppKit promotion: setting the policy before the run
            # loop starts is dropped on macOS 13-15, so schedule it on the
            # main thread to land once the run loop is actually running.
            try:
                import AppKit
                import objc

                class _PolicySetter(AppKit.NSObject):
                    @objc.namedSelector(b"apply:")
                    def apply_(self, _sender):
                        try:
                            AppKit.NSApp.setActivationPolicy_(1)  # Accessory
                            AppKit.NSApp.activateIgnoringOtherApps_(True)
                        except Exception:
                            pass

                setter = _PolicySetter.alloc().init()
                setter.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "apply:", None, False
                )
                self._policy_setter = setter
            except Exception:
                pass

    def run(self) -> None:
        if sys.platform == "darwin":
            promote_to_ui_element()
        self.icon.run(self.setup)


def claim_instance() -> bool:
    """Take the single tray slot, or report that one is already running.

    Without this, launching the tray from the app bundle and from the
    command line would leave two icons in the menu bar fighting over the
    same log and state file.
    """
    paths.ensure_data_dirs()
    lock = paths.run_dir() / "tray.pid"
    try:
        owner = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        owner = 0
    if owner and owner != os.getpid() and runtime.pid_alive(owner):
        return False
    try:
        lock.write_text("%d\n" % os.getpid(), encoding="utf-8")
    except OSError:
        return True
    return True


def release_instance() -> None:
    paths.remove_quietly(paths.run_dir() / "tray.pid")


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
    # First run: put the icon and the menu bar entry in place, quietly.
    try:
        assets.register()
    except Exception:
        pass
    # macOS: promote to a UIElement app as early as possible, before any
    # Cocoa/AppKit usage, so the status item renders and no Dock icon shows.
    if sys.platform == "darwin":
        promote_to_ui_element()
    # Hand over to the app bundle so macOS shows vpncli rather than Python
    # in the menu bar, Activity Monitor and Login Items.
    if sys.platform == "darwin" and not os.environ.get("VPNCLI_TRAY_LAUNCHED"):
        if assets.launch_macos_app():
            return 0
    if not claim_instance():
        print("vpncli tray is already running in the menu bar", file=sys.stderr)
        return 0
    try:
        app = TrayApp()
    except Exception as exc:
        release_instance()
        print("could not start the tray icon: %s" % exc, file=sys.stderr)
        return 1
    try:
        app.run()
    finally:
        release_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
