#!/usr/bin/env sh
set -eu

REPO="${VPNCLI_REPO:-https://github.com/sushantadhkr/vpncli.git}"
SPEC="vpncli[gui] @ git+${REPO}"
if [ "${VPNCLI_NO_GUI:-0}" = "1" ]; then
  SPEC="vpncli @ git+${REPO}"
fi

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 (3.8 or newer) is required"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "python 3.8 or newer is required"
command -v git >/dev/null 2>&1 || die "git is required"

if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SPEC"
else
  VENV="${HOME}/.local/share/vpncli/venv"
  BIN="${HOME}/.local/bin"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --upgrade "$SPEC"
  mkdir -p "$BIN"
  ln -sf "$VENV/bin/vpncli" "$BIN/vpncli"
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) printf 'add %s to your PATH to use vpncli\n' "$BIN" ;;
  esac
fi

if command -v vpncli >/dev/null 2>&1; then
  vpncli doctor || true
fi
printf 'vpncli installed. Run: vpncli --help\n'
printf 'To remove it later: vpncli uninstall\n'
