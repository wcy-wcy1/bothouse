#!/usr/bin/env bash
set -euo pipefail

with_mlx=false
with_shim=false
skip_install=false
dry_run=false
for flag in "$@"; do
    case "$flag" in
        --with-mlx) with_mlx=true ;;
        --with-ollama-shim) with_shim=true ;;
        --skip-install) skip_install=true ;;
        --dry-run) dry_run=true ;;
        -h|--help)
            echo 'Usage: install-bothouse-mac.sh [--with-mlx] [--with-ollama-shim] [--skip-install] [--dry-run]'
            exit 0 ;;
        *) printf 'Unknown option: %s\n' "$flag" >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != Darwin ]]; then
    echo 'This installer requires macOS.' >&2
    exit 1
fi
arch="$(uname -m)"
case "$arch" in
    arm64|x86_64) ;;
    *) printf 'Unsupported Mac architecture: %s\n' "$arch" >&2; exit 1 ;;
esac
if "$with_mlx" && [[ "$arch" != arm64 ]]; then
    echo 'MLX requires a native arm64 shell on Apple Silicon. On Intel, use --with-ollama-shim.' >&2
    exit 1
fi

kit_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PATH="${INSTALL_DIR:-$HOME/.local/bin}:/opt/homebrew/bin:/usr/local/bin:$PATH"
python_bin=
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null && \
        "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        python_bin="$(command -v "$candidate")"
        break
    fi
done
if [[ -z "$python_bin" ]]; then
    echo 'Python 3.11+ is required. Try brew install python or uv python install 3.11, then make python3 available on PATH.' >&2
    exit 1
fi

if ! command -v bothouse >/dev/null; then
    if "$skip_install"; then
        echo 'Bothouse was not found on PATH. Install it or rerun without --skip-install.' >&2
        exit 1
    elif "$dry_run"; then
        echo 'Would install Bothouse with the official install.sh.'
    else
        curl -fsSL https://raw.githubusercontent.com/zuiho-kai/bothouse/main/install.sh | sh
        hash -r
    fi
fi
if "$dry_run"; then
    echo 'Would run bothouse setup clis.'
else
    bothouse setup clis
fi

install_agent() {
    local label="$1"
    shift
    local plist="$HOME/Library/LaunchAgents/$label.plist"
    local domain="gui/$(id -u)"
    if "$dry_run"; then
        printf 'Would write %s and bootstrap %s.\n' "$plist" "$domain"
        return
    fi
    "$python_bin" - "$plist" "$label" "$@" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

path, label, *command = sys.argv[1:]
path = Path(path)
path.parent.mkdir(parents=True, exist_ok=True)
environment = {"PATH": os.environ["PATH"]}
keys = ("MLX_JEV_MODEL", "MLX_JEV_PORT") if label.endswith(".mlx-jev") else (
    "OLLAMA_BASE_URL", "OLLAMA_JEV_TOKEN"
)
for key in keys:
    if key in os.environ:
        environment[key] = os.environ[key]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "wb") as stream:
    os.fchmod(stream.fileno(), 0o600)
    plistlib.dump({"Label": label, "ProgramArguments": command,
                   "EnvironmentVariables": environment, "RunAtLoad": True,
                   "KeepAlive": True, "ThrottleInterval": 10}, stream)
PY
    if launchctl print "$domain/$label" >/dev/null 2>&1; then
        launchctl bootout "$domain/$label"
    fi
    launchctl bootstrap "$domain" "$plist"
}

if "$with_mlx"; then
    if ! command -v mlx_lm.server >/dev/null && [[ ! -x "$HOME/.local/bin/mlx_lm.server" ]]; then
        if "$dry_run"; then
            echo 'Would run uv tool install mlx-lm.'
        else
            command -v uv >/dev/null || { echo 'Install uv first with brew install uv.' >&2; exit 1; }
            uv tool install mlx-lm
            export PATH="$(uv tool dir --bin):$PATH"
        fi
    fi
    install_agent com.bothouse.mlx-jev /bin/bash "$kit_dir/mac/start-mlx-jev.sh"
fi
if "$with_shim"; then
    install_agent com.bothouse.ollama-jev-shim "$python_bin" "$kit_dir/ollama_jev_shim.py" \
        --host 127.0.0.1 --port 11435
fi

manifest="$HOME/.bothouse/pure-discord/team.toml"
printf '\n%s\n' 'Next steps'
printf '  %q %q init-manifest\n' "$python_bin" "$kit_dir/setup_discord.py"
printf '  Edit %s with the server ID and both bot tokens or token_env settings.\n' "$manifest"
printf '  %q %q apply %q --dry-run\n' "$python_bin" "$kit_dir/setup_discord.py" "$manifest"
printf '  %q %q apply %q\n' "$python_bin" "$kit_dir/setup_discord.py" "$manifest"
printf '%s\n' '  Open the invite URLs to add the bots to your server.' '  bothouse daemon install'
