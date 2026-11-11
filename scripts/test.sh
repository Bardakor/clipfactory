#!/usr/bin/env bash
# Runs every automated check. Live OpenRouter tests are skipped unless a key is configured.
#   scripts/test.sh          fast + slow backend tests, renderer + web unit tests, type checks, lint
#   scripts/test.sh --e2e    also runs the Playwright browser test (starts the stack in TEST mode)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "── backend: ruff + pytest"; (cd "$ROOT/apps/api" && uv run ruff check . && uv run pytest -q -m "not live")
echo "── prettier"; (cd "$ROOT" && pnpm format:check)
echo "── renderer: tsc + vitest"; (cd "$ROOT/apps/renderer" && pnpm typecheck && pnpm test)
echo "── web: tsc + eslint + vitest"; (cd "$ROOT/apps/web" && pnpm typecheck && pnpm lint && pnpm test)
if [ "${1:-}" = "--e2e" ]; then echo "── browser e2e (test adapters)"; (cd "$ROOT/apps/web" && pnpm e2e); fi
echo "All checks passed."
