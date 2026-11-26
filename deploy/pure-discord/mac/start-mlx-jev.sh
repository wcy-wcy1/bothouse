#!/usr/bin/env bash
set -euo pipefail

MLX_BIN="$(command -v mlx_lm.server || true)"
if [[ -z "$MLX_BIN" && -x "$HOME/.local/bin/mlx_lm.server" ]]; then
    MLX_BIN="$HOME/.local/bin/mlx_lm.server"
fi
if [[ -z "$MLX_BIN" ]]; then
    echo 'mlx_lm.server was not found. Install it with uv tool install mlx-lm.' >&2
    exit 1
fi

exec "$MLX_BIN" --model "${MLX_JEV_MODEL:-mlx-community/Qwen3.5-4B-MLX-4bit}" \
    --host 127.0.0.1 --port "${MLX_JEV_PORT:-9941}"
