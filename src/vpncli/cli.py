from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import platform
import random
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__, assets, netutil, paths, privilege, runtime, settings, uninstall, update
from .profiles import Profile, ProfileError, ProfileStore, slugify
from .runtime import Status, VpnError

WINDOWS = sys.platform.startswith("win")
USE_COLOR = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and (not WINDOWS or bool(os.environ.get("WT_SESSION")))
)
STATE_COLORS = {
    "connected": "32",
    "connecting": "33",
    "reconnecting": "33",
    "disconnecting": "33",
    "auth_failed": "31",
    "error": "31",
    "disconnected": "90",
}


def paint(text: str, code: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else text


def good(message: str) -> None:
    print("%s %s" % (paint("ok", "32"), message))


def warn(message: str) -> None:
    print("%s %s" % (paint("warning:", "33"), message), file=sys.stderr)


def fail(message: str) -> int:
    print("%s %s" % (paint("error:", "31"), message), file=sys.stderr)
    return 1


def confirm(question: str) -> bool:
    try:
        return input("%s [y/N] " % question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def prompt_credentials() -> Tuple[str, str]:
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    if not username or not password:
        raise ProfileError("username and password are required")
    return username, password


def collect_credentials(args: argparse.Namespace) -> Optional[Tuple[str, str]]:
    if getattr(args, "ask", False):
        return prompt_credentials()
    username = getattr(args, "username", None)
    password = getattr(args, "password", None)
    if username is None and password is None:
        return None
    if username is None:
        raise ProfileError("--password requires --username")
    if password is None:
        password = getpass.getpass("Password for %s: " % username)
    return username, password


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%dh %02dm %02ds" % (hours, minutes, secs)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


def is_interactive(args: argparse.Namespace) -> bool:
    return not getattr(args, "non_interactive", False) and sys.stdin.isatty()


def extra_options(args: argparse.Namespace) -> List[str]:
    options: List[str] = []
    for item in getattr(args, "opt", None) or []:
        options.extend(shlex.split(item))
    return options


def print_table(headers: List[str], rows: List[List[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(paint(line, "1"))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def render_status(status: Status) -> str:
    label = status.state.replace("_", " ")
    rows = [("State", paint(label, STATE_COLORS.get(status.state, "0")))]
    if status.profile:
        rows.append(("Profile", status.profile))
    if status.server:
        rows.append(("Server", status.server))
    if status.local_ip:
        rows.append(("Local IP", status.local_ip))
    if status.device:
        rows.append(("Device", status.device))
    if status.state == "connected":
        rows.append(("Uptime", format_duration(status.uptime)))
    if status.pid:
        rows.append(("PID", str(status.pid)))
    if status.detail:
        rows.append(("Detail", status.detail))
    return "\n".join("%-9s %s" % (key + ":", value) for key, value in rows)


def measure_profiles(profiles: List[Profile]) -> List[Tuple[Profile, Optional[float]]]:
    if not profiles:
        return []
    workers = min(16, len(profiles))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda p: (p, netutil.profile_latency(p.info())), profiles))


def cmd_add(args: argparse.Namespace, store: ProfileStore) -> int:
    credentials = collect_credentials(args)
    username, password = credentials if credentials else (None, None)
    profile, warnings = store.add(
        args.name, Path(args.file), username, password, overwrite=args.force
    )
    for message in warnings:
        warn(message)
    good("added profile '%s'%s" % (profile.name, " with saved credentials" if credentials else ""))
    return 0


def cmd_import(args: argparse.Namespace, store: ProfileStore) -> int:
    source = Path(args.path).expanduser()
    if source.is_file():
        files = [source]
    elif source.is_dir():
        candidates = source.rglob("*") if args.recursive else source.iterdir()
        files = sorted(p for p in candidates if p.is_file() and p.suffix.lower() == ".ovpn")
    else:
        return fail("no such file or directory: %s" % source)
    if not files:
        return fail("no .ovpn files found in %s" % source)

    credentials = collect_credentials(args)
    username, password = credentials if credentials else (None, None)
    existing = set(store.names())
    imported = 0
    for path in files:
        name = slugify(path.stem)
        if name in existing and not args.force:
            counter = 2
            if not args.rename_duplicates:
                warn("skipped %s: profile '%s' exists (use --force)" % (path.name, name))
                continue
            while "%s-%d" % (name, counter) in existing:
                counter += 1
            name = "%s-%d" % (name, counter)
        try:
            profile, warnings = store.add(name, path, username, password, overwrite=args.force)
        except ProfileError as exc:
            warn("skipped %s: %s" % (path.name, exc))
            continue
        existing.add(profile.name)
        imported += 1
        for message in warnings:
            warn("%s: %s" % (path.name, message))
        good("imported %s as '%s'" % (path.name, profile.name))
    print("%d of %d profile(s) imported" % (imported, len(files)))
    return 0 if imported else 1


def cmd_list(args: argparse.Namespace, store: ProfileStore) -> int:
    profiles = store.all()
    session = runtime.active_session()
    default = settings.get("default_profile")
    records = []
    for profile in profiles:
        try:
            info = profile.info()
            remote = str(info.remotes[0]) if info.remotes else "-"
            extra = len(info.remotes) - 1
            if extra > 0:
                remote += " (+%d)" % extra
            needs_auth = info.needs_auth
        except (OSError, IndexError):
            remote, needs_auth = "-", False
        records.append(
            {
                "name": profile.name,
                "remote": remote,
                "credentials": profile.has_credentials,
                "needs_credentials": needs_auth,
                "active": bool(session and session.profile == profile.name),
                "default": profile.name == default,
            }
        )
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print("No profiles yet. Add one with: vpncli add NAME FILE.ovpn")
        return 0
    rows = []
    for item in records:
        flags = ("*" if item["active"] else " ") + ("d" if item["default"] else " ")
        if item["credentials"]:
            creds = "saved"
        elif item["needs_credentials"]:
            creds = "missing"
        else:
            creds = "-"
        rows.append([flags, item["name"], item["remote"], creds])
    print_table(["", "NAME", "REMOTE", "CREDENTIALS"], rows)
    print("\n* connected   d default")
    return 0


def cmd_show(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = store.get(args.name)
    info = profile.info()
    if profile.has_credentials:
        auth = "saved (user: %s)" % (profile.username() or "?")
    elif info.needs_auth:
        auth = "required, not saved"
    else:
        auth = "not required"
    print("name:     %s" % profile.name)
    print("path:     %s" % profile.config_path)
    print("default:  %s" % ("yes" if settings.get("default_profile") == profile.name else "no"))
    print("auth:     %s" % auth)
    print("remotes:")
    for remote in info.remotes:
        print("  - %s" % remote)
    if info.scripts:
        print("scripts:  %s (review before connecting)" % ", ".join(info.scripts))
    return 0


def cmd_remove(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = store.get(args.name)
    session = runtime.active_session()
    if session and session.profile == profile.name:
        return fail("'%s' is connected; run: vpncli disconnect" % profile.name)
    if not args.yes and not confirm("Remove profile '%s'?" % profile.name):
        print("cancelled")
        return 1
    store.remove(profile.name)
    if settings.get("default_profile") == profile.name:
        settings.put("default_profile", None)
    good("removed '%s'" % profile.name)
    return 0


def cmd_rename(args: argparse.Namespace, store: ProfileStore) -> int:
    session = runtime.active_session()
    profile = store.get(args.old)
    if session and session.profile == profile.name:
        return fail("'%s' is connected; disconnect first" % profile.name)
    renamed = store.rename(profile.name, args.new)
    if settings.get("default_profile") == profile.name:
        settings.put("default_profile", renamed.name)
    good("renamed '%s' to '%s'" % (profile.name, renamed.name))
    return 0


def cmd_creds(args: argparse.Namespace, store: ProfileStore) -> int:
    if args.all:
        targets = store.all()
    elif args.name:
        targets = [store.get(args.name)]
    else:
        return fail("give a profile name or --all")
    if not targets:
        return fail("no profiles to update")
    if args.clear:
        for profile in targets:
            store.clear_credentials(profile.name)
        good("cleared credentials for %d profile(s)" % len(targets))
        return 0
    credentials = collect_credentials(args) or prompt_credentials()
    for profile in targets:
        store.set_credentials(profile.name, credentials[0], credentials[1])
    good("saved credentials for %d profile(s)" % len(targets))
    return 0


def cmd_default(args: argparse.Namespace, store: ProfileStore) -> int:
    if args.clear:
        settings.put("default_profile", None)
        good("default profile cleared")
        return 0
    if not args.name:
        current = settings.get("default_profile")
        print(current if current else "no default profile set")
        return 0
    profile = store.get(args.name)
    settings.put("default_profile", profile.name)
    good("default profile is now '%s'" % profile.name)
    return 0


def choose_profile(args: argparse.Namespace, store: ProfileStore) -> Profile:
    profiles = store.all()
    if args.fastest:
        if not profiles:
            raise ProfileError("no profiles to choose from")
        print("Measuring latency for %d profile(s)..." % len(profiles))
        results = [(p, ms) for p, ms in measure_profiles(profiles) if ms is not None]
        if not results:
            raise ProfileError("no server answered; try naming a profile explicitly")
        best, latency = min(results, key=lambda item: item[1])
        print("Fastest: %s (%.0f ms)" % (best.name, latency))
        return best
    if args.random:
        if not profiles:
            raise ProfileError("no profiles to choose from")
        return random.choice(profiles)
    if args.name:
        return store.get(args.name)
    default = settings.get("default_profile")
    if default:
        return store.get(str(default))
    if len(profiles) == 1:
        return profiles[0]
    raise ProfileError("choose a profile, or set one with: vpncli default NAME")


def obtain_credentials(
    store: ProfileStore, profile: Profile, interactive: bool, save: bool
) -> Optional[Tuple[str, str]]:
    if profile.has_credentials or not profile.info().needs_auth:
        return None
    if not interactive:
        raise VpnError(
            "profile '%s' needs credentials; run: vpncli creds %s" % (profile.name, profile.name)
        )
    credentials = prompt_credentials()
    if save or confirm("Save these credentials for '%s'?" % profile.name):
        store.set_credentials(profile.name, credentials[0], credentials[1])
        return None
    return credentials


def connect_profile(
    store: ProfileStore,
    profile: Profile,
    timeout: float,
    interactive: bool,
    save: bool,
    extra: List[str],
) -> None:
    credentials = obtain_credentials(store, profile, interactive, save)
    print("Connecting to %s ..." % profile.name)
    announced: List[Optional[str]] = [None]

    def progress(state: str) -> None:
        if state != announced[0]:
            announced[0] = state
            print("  %s" % state.replace("_", " "))

    runtime.connect(
        profile,
        credentials=credentials,
        timeout=timeout,
        interactive=interactive,
        extra=extra,
        progress=progress,
    )
    settings.put("last_profile", profile.name)
    status = runtime.get_status()
    good("connected to %s" % profile.name)
    for label, value in (("server", status.server), ("local ip", status.local_ip), ("device", status.device)):
        if value:
            print("  %-9s %s" % (label, value))


def cmd_connect(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = choose_profile(args, store)
    interactive = is_interactive(args)
    current = runtime.active_session()
    if current is not None:
        if current.profile == profile.name:
            good("already connected to %s" % profile.name)
            return 0
        print("Disconnecting from %s ..." % current.profile)
        runtime.disconnect(interactive=interactive)
    if args.foreground:
        credentials = obtain_credentials(store, profile, interactive, args.save)
        return runtime.run_foreground(
            profile, credentials=credentials, interactive=interactive, extra=extra_options(args)
        )
    connect_profile(store, profile, args.timeout, interactive, args.save, extra_options(args))
    if args.show_ip:
        address = netutil.public_ip()
        print("  %-9s %s" % ("public ip", address or "unavailable"))
    return 0


def cmd_disconnect(args: argparse.Namespace, store: ProfileStore) -> int:
    name = runtime.disconnect(interactive=is_interactive(args))
    if name is None:
        print("not connected")
        return 0
    good("disconnected from %s" % name)
    return 0


def cmd_reconnect(args: argparse.Namespace, store: ProfileStore) -> int:
    interactive = is_interactive(args)
    session = runtime.active_session()
    name = session.profile if session else settings.get("last_profile")
    if not name:
        return fail("nothing to reconnect to; run: vpncli connect NAME")
    profile = store.get(str(name))
    if session:
        print("Disconnecting from %s ..." % session.profile)
        runtime.disconnect(interactive=interactive)
    connect_profile(store, profile, args.timeout, interactive, False, extra_options(args))
    return 0


def cmd_status(args: argparse.Namespace, store: ProfileStore) -> int:
    if args.watch:
        try:
            while True:
                status = runtime.get_status()
                print("\033[2J\033[H" + render_status(status), flush=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            return 0
    status = runtime.get_status()
    if args.json:
        print(json.dumps(status.as_dict(), indent=2))
    else:
        print(render_status(status))
    return 0 if status.state == "connected" else 1


def cmd_logs(args: argparse.Namespace, store: ProfileStore) -> int:
    for line in runtime.tail_log(args.lines):
        print(line)
    if args.follow:
        try:
            for line in runtime.follow_log():
                print(line, flush=True)
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_ip(args: argparse.Namespace, store: ProfileStore) -> int:
    address = netutil.public_ip()
    if address is None:
        return fail("could not determine the public IP address")
    print(address)
    return 0


def cmd_latency(args: argparse.Namespace, store: ProfileStore) -> int:
    profiles = [store.get(name) for name in args.names] if args.names else store.all()
    if not profiles:
        return fail("no profiles to test")
    results = measure_profiles(profiles)
    results.sort(key=lambda item: (item[1] is None, item[1] or 0.0))
    rows = [
        [p.name, "%.0f ms" % ms if ms is not None else "no response"] for p, ms in results
    ]
    print_table(["NAME", "LATENCY"], rows)
    return 0


def cmd_doctor(args: argparse.Namespace, store: ProfileStore) -> int:
    problems = 0

    def report(label: str, passed: bool, detail: str = "", required: bool = True) -> None:
        nonlocal problems
        mark = paint("ok", "32") if passed else paint("fail" if required else "warn", "31" if required else "33")
        print("[%s] %s%s" % (mark, label, (": " + detail) if detail else ""))
        if not passed and required:
            problems += 1

    try:
        binary = runtime.find_openvpn()
        output = subprocess.run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        ).stdout
        report("openvpn", True, "%s (%s)" % (binary, (output or "").splitlines()[0] if output else "version unknown"))
    except (VpnError, OSError) as exc:
        report("openvpn", False, str(exc))

    if WINDOWS:
        report("administrator rights", privilege.is_admin(), "needed to connect; open an elevated terminal", required=False)
    elif paths.is_root():
        report("privileges", True, "running as root")
    else:
        report("sudo", shutil.which("sudo") is not None, "needed to start openvpn")

    if sys.platform.startswith("linux"):
        report("/dev/net/tun", os.path.exists("/dev/net/tun"), "tun device")

    report("data directory", True, str(paths.data_dir()))
    report("profiles", True, "%d stored" % len(store.names()))
    if assets.has_artwork():
        report("app icon", True, str(assets.icon_png()))
        if sys.platform == "darwin":
            launcher = str(Path.home() / "Applications" / "vpncli.app")
        elif sys.platform.startswith("win"):
            launcher = "Start Menu: vpncli tray.lnk"
        else:
            launcher = str(assets.LINUX_APPLICATIONS / "vpncli-tray.desktop")
        report("app registration", assets.is_registered(), launcher, required=False)
    else:
        report("app icon", False, "artwork missing from the installation", required=False)
    gui_ready = all(
        importlib.util.find_spec(name) is not None for name in ("pystray", "PIL")
    )
    report("tray dependencies", gui_ready, "install with: pip install 'vpncli[gui]'" if not gui_ready else "", required=False)
    report("internet", netutil.public_ip(timeout=4.0) is not None, required=False)
    return 1 if problems else 0


def cmd_tray(args: argparse.Namespace, store: ProfileStore) -> int:
    from . import tray

    return tray.main()


def cmd_install_helper(args: argparse.Namespace, store: ProfileStore) -> int:
    from . import privilege, paths

    # If not admin, elevate this same command.
    if not privilege.is_admin():
        code, output = privilege.run_elevated(["install-helper"])
        if code != 0:
            return fail(output or "could not install helper")
        print(output or "helper installed")
        return 0

    # Running as root: install the launchd/systemd service.
    import os
    import sys
    from pathlib import Path

    if sys.platform == "darwin":
        plist_path = Path("/Library/LaunchDaemons/com.vpncli.helper.plist")
        python = sys.executable
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\\n'
            '<plist version="1.0"><dict>\\n'
            "  <key>Label</key><string>com.vpncli.helper</string>\\n"
            "  <key>ProgramArguments</key><array>\\n"
            "    <string>%s</string><string>-m</string><string>vpncli.helper</string>"
            "<string>--foreground</string>\\n"
            "  </array>\\n"
            "  <key>RunAtLoad</key><true/>\\n"
            "  <key>KeepAlive</key><true/>\\n"
            "  <key>StandardOutPath</key><string>/var/log/vpncli-helper.log</string>\\n"
            "  <key>StandardErrorPath</key><string>/var/log/vpncli-helper.log</string>\\n"
            "</dict></plist>\\n" % python
        )
        plist_path.write_text(plist, encoding="utf-8")
        os.chmod(str(plist_path), 0o644)
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=False)
        print("installed helper to %s" % plist_path)
        return 0
    if sys.platform.startswith("linux"):
        unit_path = Path("/etc/systemd/system/vpncli-helper.service")
        python = sys.executable
        unit = (
            "[Unit]\\nDescription=vpncli privileged helper\\nAfter=network.target\\n\\n"
            "[Service]\\nType=simple\\nExecStart=%s -m vpncli.helper --foreground\\n"
            "Restart=always\\nRestartSec=2\\n\\n"
            "[Install]\\nWantedBy=multi-user.target\\n" % python
        )
        unit_path.write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", "vpncli-helper"], check=False)
        print("installed helper to %s" % unit_path)
        return 0
    return fail("helper install not yet supported on this platform")


def cmd_update(args: argparse.Namespace, store: ProfileStore) -> int:
    session = runtime.active_session()
    if session is not None and args.disconnect:
        print("Disconnecting from %s ..." % session.profile)
        runtime.disconnect(interactive=is_interactive(args))
    try:
        url, output = update.update(no_gui=args.no_gui, dry_run=args.check)
    except update.UpdateError as exc:
        return fail(str(exc))
    if args.check:
        print("update runs the installer from %s" % url)
        return 0
    print("ran %s" % url)
    for line in (output or "").splitlines():
        if line.strip():
            print("  %s" % line.strip())
    good("vpncli updated; run: vpncli --version")
    return 0


def cmd_register(args: argparse.Namespace, store: ProfileStore) -> int:
    if not assets.has_artwork():
        return fail("the application icon is missing from this installation")
    try:
        created = assets.register(force=args.force)
    except (OSError, assets.RegisterError) as exc:
        return fail("could not register the application: %s" % exc)
    if created is None:
        good("already registered; nothing to do")
    else:
        good("registered %s" % created)
    print("  %-9s %s" % ("icon", assets.icon_png()))
    return 0


def cmd_uninstall(args: argparse.Namespace, store: ProfileStore) -> int:
    try:
        steps = uninstall.plan()
    except uninstall.UninstallError as exc:
        return fail(str(exc))
    data = uninstall.data_dir()
    print("This will remove the vpncli command, the tray icon and:")
    for label, _ in steps:
        print("  - %s" % label)
    if args.keep_data:
        print("  - nothing else (data kept at %s)" % data)
    else:
        print("  - profiles, saved credentials, settings and logs in %s" % data)
    if not args.yes and not confirm("Uninstall vpncli?"):
        print("cancelled")
        return 1
    session = runtime.active_session()
    if session is not None:
        runtime.disconnect(interactive=is_interactive(args))
    try:
        _, removed, scheduled = uninstall.run(keep_data=args.keep_data)
    except uninstall.UninstallError as exc:
        return fail(str(exc))
    if removed is not None:
        good("removed %s" % removed)
    else:
        print("kept %s" % data)
    if scheduled:
        good("vpncli and the tray icon will be removed as soon as this command exits")
    else:
        warn("could not schedule the removal; remove it with: pipx uninstall %s" % uninstall.PACKAGE)
    return 0


def cmd_paths(args: argparse.Namespace, store: ProfileStore) -> int:
    print("data:      %s" % paths.data_dir())
    print("profiles:  %s" % paths.profiles_dir())
    print("log:       %s" % paths.log_file())
    print("state:     %s" % paths.state_file())
    print("system:    %s %s" % (platform.system(), platform.release()))
    return 0


def add_credential_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--username", help="username for auth-user-pass")
    parser.add_argument("-p", "--password", help="password (visible in shell history; prefer --ask)")
    parser.add_argument("-a", "--ask", action="store_true", help="prompt for username and password")


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-t", "--timeout", type=float, default=45.0, help="seconds to wait for the tunnel (default 45)")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt (for scripts and the tray)")
    parser.add_argument("-o", "--opt", action="append", metavar="OPTION", help="extra openvpn option, e.g. --opt '--verb 5' (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vpncli",
        description="OpenVPN command line client with a system tray status icon.",
    )
    parser.add_argument("--version", action="version", version="vpncli " + __version__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub = commands.add_parser("add", help="import one .ovpn file as a named profile")
    sub.add_argument("name")
    sub.add_argument("file")
    sub.add_argument("-f", "--force", action="store_true", help="replace an existing profile")
    add_credential_options(sub)
    sub.set_defaults(func=cmd_add)

    sub = commands.add_parser("import", help="import a .ovpn file or every .ovpn in a folder")
    sub.add_argument("path")
    sub.add_argument("-r", "--recursive", action="store_true", help="search sub-folders")
    sub.add_argument("-f", "--force", action="store_true", help="replace profiles with the same name")
    sub.add_argument("--rename-duplicates", action="store_true", help="add -2, -3 ... when a name exists")
    add_credential_options(sub)
    sub.set_defaults(func=cmd_import)

    sub = commands.add_parser("list", aliases=["ls"], help="list profiles")
    sub.add_argument("--json", action="store_true")
    sub.set_defaults(func=cmd_list)

    sub = commands.add_parser("show", help="show profile details")
    sub.add_argument("name")
    sub.set_defaults(func=cmd_show)

    sub = commands.add_parser("remove", aliases=["rm"], help="delete a profile")
    sub.add_argument("name")
    sub.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sub.set_defaults(func=cmd_remove)

    sub = commands.add_parser("rename", help="rename a profile")
    sub.add_argument("old")
    sub.add_argument("new")
    sub.set_defaults(func=cmd_rename)

    sub = commands.add_parser("creds", help="save or clear credentials for a profile")
    sub.add_argument("name", nargs="?")
    sub.add_argument("--all", action="store_true", help="apply to every profile")
    sub.add_argument("--clear", action="store_true", help="delete saved credentials")
    add_credential_options(sub)
    sub.set_defaults(func=cmd_creds)

    sub = commands.add_parser("default", help="show or set the default profile")
    sub.add_argument("name", nargs="?")
    sub.add_argument("--clear", action="store_true")
    sub.set_defaults(func=cmd_default)

    sub = commands.add_parser("connect", aliases=["up"], help="connect to a profile")
    sub.add_argument("name", nargs="?", help="profile name (defaults to the default profile)")
    sub.add_argument("--fastest", action="store_true", help="pick the profile with the lowest latency")
    sub.add_argument("--random", action="store_true", help="pick a random profile")
    sub.add_argument("--foreground", action="store_true", help="run in this terminal; Ctrl-C disconnects")
    sub.add_argument("--save", action="store_true", help="save prompted credentials without asking")
    sub.add_argument("--show-ip", action="store_true", help="print the public IP after connecting")
    add_runtime_options(sub)
    sub.set_defaults(func=cmd_connect)

    sub = commands.add_parser("disconnect", aliases=["down"], help="disconnect the active VPN")
    sub.add_argument("--non-interactive", action="store_true")
    sub.set_defaults(func=cmd_disconnect)

    sub = commands.add_parser("reconnect", help="reconnect to the current or last profile")
    add_runtime_options(sub)
    sub.set_defaults(func=cmd_reconnect)

    sub = commands.add_parser("status", help="show connection status (exit code 0 when connected)")
    sub.add_argument("--json", action="store_true")
    sub.add_argument("-w", "--watch", action="store_true", help="refresh every second")
    sub.set_defaults(func=cmd_status)

    sub = commands.add_parser("logs", help="show the openvpn log")
    sub.add_argument("-n", "--lines", type=int, default=50)
    sub.add_argument("-f", "--follow", action="store_true")
    sub.set_defaults(func=cmd_logs)

    sub = commands.add_parser("ip", help="print the current public IP address")
    sub.set_defaults(func=cmd_ip)

    sub = commands.add_parser("latency", help="measure latency to profile servers")
    sub.add_argument("names", nargs="*")
    sub.set_defaults(func=cmd_latency)

    sub = commands.add_parser("doctor", help="check the environment")
    sub.set_defaults(func=cmd_doctor)

    sub = commands.add_parser("tray", help="start the system tray status icon")
    sub.set_defaults(func=cmd_tray)

    sub = commands.add_parser("install-helper", help="install the privileged helper daemon (requires admin)")
    sub.set_defaults(func=cmd_install_helper)

    sub = commands.add_parser(
        "update", help="update vpncli and the tray icon in place from GitHub"
    )
    sub.add_argument("--check", action="store_true", help="show which installer would be used and stop")
    sub.add_argument("--no-gui", action="store_true", help="update without the tray dependencies")
    sub.add_argument("--no-disconnect", dest="disconnect", action="store_false", help="update while connected")
    sub.set_defaults(func=cmd_update, disconnect=True)

    sub = commands.add_parser(
        "register", help="install the app icon and menu-bar/launcher entry"
    )
    sub.add_argument("-f", "--force", action="store_true", help="register again even if already done")
    sub.set_defaults(func=cmd_register)

    sub = commands.add_parser(
        "uninstall", help="remove the vpncli command, the tray icon and local data"
    )
    sub.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sub.add_argument(
        "--keep-data", action="store_true", help="remove the program but keep profiles and credentials"
    )
    sub.set_defaults(func=cmd_uninstall)

    sub = commands.add_parser("paths", help="show where vpncli stores its data")
    sub.set_defaults(func=cmd_paths)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    store = ProfileStore()
    try:
        return int(args.func(args, store) or 0)
    except (ProfileError, VpnError) as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return 130
