#!/usr/bin/env bash
# Verifies everything CLIPFACTORY needs on this machine. Exits non-zero if something required is missing.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ok=1
check() { # name, command, required(1/0), hint
  if out="$(eval "$2" 2>/dev/null | head -1)" && [ -n "$out" ]; then
    printf "  ✓ %-14s %s\n" "$1" "$out"
  elif [ "$3" = "1" ]; then
    printf "  ✗ %-14s MISSING — %s\n" "$1" "$4"; ok=0
  else
    printf "  – %-14s not found (optional) — %s\n" "$1" "$4"
  fi
}
echo "CLIPFACTORY dependency check"
check "node"       "node --version"                         1 "install Node.js 20+ (brew install node)"
check "pnpm"       "pnpm --version"                         1 "npm install -g pnpm"
check "uv"         "uv --version"                           1 "brew install uv"
check "python3.12" "uv python find 3.12"                    1 "uv python install 3.12"
check "ffmpeg"     "ffmpeg -version"                        1 "brew install ffmpeg"
check "ffprobe"    "ffprobe -version"                       1 "brew install ffmpeg"
check "arch"       "uname -m | grep arm64"                  0 "MLX Whisper needs Apple Silicon; use the OpenRouter transcriber otherwise"
if [ -d "$ROOT/apps/api/.venv" ]; then
  check "yt-dlp"      "cd '$ROOT/apps/api' && uv run --no-sync python -m yt_dlp --version"                              1 "cd apps/api && uv sync"
  check "mlx-whisper" "cd '$ROOT/apps/api' && uv run --no-sync python -c 'import mlx_whisper, mlx.core as m; print(\"mlx\", m.__version__)'" 0 "cd apps/api && uv sync (Apple Silicon only)"
  check "opencv"      "cd '$ROOT/apps/api' && uv run --no-sync python -c 'import cv2; print(cv2.__version__)'"         1 "cd apps/api && uv sync"
else
  echo "  – python deps    not installed yet — run: cd apps/api && uv sync"
fi
if [ -d "$ROOT/apps/renderer/node_modules/remotion" ]; then
  check "remotion"  "node -p \"require('$ROOT/apps/renderer/node_modules/remotion/package.json').version\""            1 "pnpm install"
else
  echo "  – node deps      not installed yet — run: pnpm install"
fi
if [ -f "$ROOT/.env" ] && grep -Eq '^OPENROUTER_API_KEY=.+' "$ROOT/.env"; then
  echo "  ✓ OpenRouter key configured in .env"
else
  echo "  – OpenRouter key not set — the pipeline will run in heuristic-only mode (see .env.example)"
fi
[ "$ok" = "1" ] && echo "All required dependencies are present." || { echo "Some required dependencies are missing."; exit 1; }
