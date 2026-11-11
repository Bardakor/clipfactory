#!/usr/bin/env bash
# One command to run CLIPFACTORY locally: API (127.0.0.1:8000), worker, and dashboard (localhost:3000).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/check-deps.sh"
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example — add your OPENROUTER_API_KEY there."; }
(cd apps/api && uv sync --quiet)
[ -d node_modules ] || pnpm install
API_PORT="${CLIPFACTORY_API_PORT:-8000}"
WEB_PORT="${PORT:-3000}"
pids=()
cleanup() { trap - INT TERM EXIT; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup INT TERM EXIT
# The API binds to loopback only: this is a single-user local tool with no authentication.
(cd apps/api && exec uv run --no-sync uvicorn clipfactory.api.app:app --host 127.0.0.1 --port "$API_PORT" --log-level warning) & pids+=($!)
(cd apps/api && exec uv run --no-sync python -m clipfactory.worker) & pids+=($!)
(cd apps/web && NEXT_PUBLIC_API_URL="http://127.0.0.1:$API_PORT" exec pnpm exec next dev --port "$WEB_PORT") & pids+=($!)
echo "CLIPFACTORY → http://localhost:$WEB_PORT   (API http://127.0.0.1:$API_PORT)"
wait
