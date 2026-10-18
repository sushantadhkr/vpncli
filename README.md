# vpncli

A cross-platform OpenVPN command line client for macOS, Linux and Windows, with an optional system tray (menu bar) status icon.

Import your `.ovpn` files once, save the username and password, then connect with a single command.

```console
$ vpncli import ~/Downloads/vpn-configs -u myuser -a
$ vpncli connect --fastest
$ vpncli status
$ vpncli disconnect
```

## Features

- Import single `.ovpn` files or whole folders, with referenced certificate and key files copied along
- Saved credentials per profile (stored with owner-only permissions), applied to one profile or all at once
- Connect by name, by default profile, to a random profile, or to the lowest-latency profile
- Runs in the background and survives closing the terminal, or in the foreground with Ctrl-C to stop
- Live status: state, server, tunnel IP, device and uptime, with `--json` and `--watch`
- Log viewer with follow mode, public IP check, latency test, and an environment doctor
- Switching profiles while connected disconnects the old tunnel first
- Passes extra OpenVPN options through (`--opt '--verb 5'`)
- System tray icon on macOS, Windows and Linux with connect, disconnect, reconnect and IP actions

## Requirements

- Python 3.8 or newer
- [OpenVPN](https://openvpn.net/community-downloads/) 2.5 or newer installed
  - macOS: `brew install openvpn`
  - Debian/Ubuntu: `sudo apt install openvpn`
  - Fedora: `sudo dnf install openvpn`
  - Windows: `winget install OpenVPNTechnologies.OpenVPN`
- Permission to create a tunnel: `sudo` on macOS and Linux, an elevated terminal on Windows

## Install

Replace `sushantadhkr` with the account that hosts the repository.

With pipx (recommended):

```sh
pipx install "vpncli[gui] @ git+https://github.com/sushantadhkr/vpncli.git"
```

CLI only, without the tray dependencies:

```sh
pipx install "git+https://github.com/sushantadhkr/vpncli.git"
```

One-line installers:

```sh
curl -fsSL https://raw.githubusercontent.com/sushantadhkr/vpncli/main/install.sh | sh
```

```powershell
irm https://raw.githubusercontent.com/sushantadhkr/vpncli/main/install.ps1 | iex
```

From a local clone:

```sh
git clone https://github.com/sushantadhkr/vpncli.git
cd vpncli
pipx install ".[gui]"
```

Run `vpncli doctor` afterwards to confirm everything is in place.

## Quick start

```sh
vpncli add office ~/vpn/office.ovpn -u alice -a
vpncli import ~/vpn/all-servers -u alice -p 'secret'
vpncli list
vpncli default office
vpncli connect
vpncli connect nl-amsterdam
vpncli connect --fastest
vpncli status
vpncli disconnect
```

`-a` prompts for the username and password. Passing `-p` on the command line leaves the password in your shell history, so prefer `-a`.

## Commands

| Command | What it does |
| --- | --- |
| `add NAME FILE` | Import one `.ovpn` file as a named profile (`-f` replaces, `-u -p -a` set credentials) |
| `import PATH` | Import a file or every `.ovpn` in a folder (`-r` recursive, `--rename-duplicates`) |
| `list` | List profiles with remote, credential state, active and default markers (`--json`) |
| `show NAME` | Show remotes, credential state and any scripts the config runs |
| `remove NAME` | Delete a profile (`-y` skips confirmation) |
| `rename OLD NEW` | Rename a profile |
| `creds [NAME]` | Save credentials for a profile or `--all` profiles, or `--clear` them |
| `default [NAME]` | Show or set the default profile (`--clear` removes it) |
| `connect [NAME]` | Connect in the background (`--fastest`, `--random`, `--foreground`, `--timeout`, `--show-ip`) |
| `disconnect` | Stop the active tunnel |
| `reconnect` | Disconnect and connect again to the same or last profile |
| `status` | Show state; exit code is 0 only when connected (`--json`, `--watch`) |
| `logs` | Show the OpenVPN log (`-n` lines, `-f` follow) |
| `ip` | Print the current public IP |
| `latency [NAME...]` | Measure latency to profile servers |
| `doctor` | Check OpenVPN, privileges, tun device and tray dependencies |
| `tray` | Start the system tray icon |
| `uninstall` | Remove the CLI, the tray icon and local data (`--keep-data`, `--yes`) |
| `paths` | Print where vpncli keeps its files |

Profile names can be abbreviated to any unique prefix, so `vpncli connect nl` works when only one profile starts with `nl`.

## System tray

```sh
vpncli tray
```

The icon sits in the macOS menu bar, the Windows notification area or the Linux system tray. It shows a grey shield when disconnected, amber while connecting and green with a tick when connected. The menu lists every profile under **Connect** and offers Disconnect, Reconnect, Show public IP, Open log, Open profiles folder and, at the bottom, Uninstall.

Connecting needs administrator rights, so the tray asks for them through the native prompt of each system: the macOS password dialog, polkit (`pkexec`) on Linux, and the UAC prompt on Windows. Profiles that need a username and password must have them saved first with `vpncli creds`, because the tray cannot ask for them.

Linux notes: GNOME needs the AppIndicator extension, and Debian-based systems may need `sudo apt install gir1.2-ayatanaappindicator3-0.1`. KDE, XFCE and most other desktops work out of the box.

To start the tray at login, add `vpncli tray` (or the `vpncli-tray` launcher on Windows) to your login items, autostart entries or Startup folder.

## Uninstalling

```sh
vpncli uninstall
```

This removes the `vpncli` command and the tray icon together, because both are entry points of the same package. It works out how the program was installed (pipx, the private virtual environment created by `install.sh`, or a plain `pip install`), disconnects any active tunnel, and deletes your data directory: profiles, saved credentials, settings and logs.

Pass `--keep-data` to uninstall the program but keep your profiles and credentials, or `-y` to skip the confirmation. The actual file removal is handed to a detached process and happens a moment after the command exits, so it never has to delete the interpreter that is running it.

The tray menu has the same two choices under **Uninstall vpncli...** at the bottom: **Uninstall and delete all data** and **Uninstall but keep profiles**.

## How it works

An `.ovpn` file is an OpenVPN configuration: a `remote` line naming the server, the transport (`proto udp` or `tcp`), TLS settings, and the certificates and keys either as separate files or inline in `<ca>`, `<cert>` and `<key>` blocks. If it contains `auth-user-pass` with no file name, OpenVPN asks for a username and password at startup.

vpncli stores each profile in its own folder and starts OpenVPN like this:

```text
openvpn --cd <profile dir> --config config.ovpn --auth-user-pass auth.txt \
        --writepid <pidfile> --log <logfile> --daemon
```

- `--cd` makes relative certificate paths resolve inside the profile folder
- `auth.txt` is a two-line file (username, password) with owner-only permissions, the format OpenVPN reads for `--auth-user-pass`
- `--daemon` detaches OpenVPN after startup. Because a daemon cannot prompt, credentials are always supplied up front
- vpncli reads the log to learn the state: `Initialization Sequence Completed` means connected, `AUTH_FAILED` means bad credentials, a restart line means reconnecting
- Disconnecting sends SIGTERM to the recorded process id (`taskkill` on Windows), which lets OpenVPN remove its routes cleanly

On Windows OpenVPN has no daemon mode, so vpncli starts it as a detached background process instead.

## Where files live

| Platform | Location |
| --- | --- |
| macOS | `~/Library/Application Support/vpncli` |
| Linux | `~/.config/vpncli` |
| Windows | `%APPDATA%\vpncli` |

Set `VPNCLI_HOME` to use another folder, and `VPNCLI_OPENVPN` to point at a specific OpenVPN binary.

## Security notes

- Credentials are saved in plain text with `0600` permissions, which is how OpenVPN consumes them. Anyone with access to your account can read them
- OpenVPN runs as root. Configs that contain `up`, `down`, `plugin` or similar directives can execute commands, so `vpncli add` warns about them. Only import configs you trust
- vpncli does not add a kill switch. If the tunnel drops, traffic can leave through your normal connection unless your config or firewall prevents it

## Development

```sh
git clone https://github.com/sushantadhkr/vpncli.git
cd vpncli
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[gui]"
python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
