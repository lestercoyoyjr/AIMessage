#!/usr/bin/env bash
# Self-contained runner for the forget->revoke live smoke (demo/smoke_forget_revoke.py).
#
# Boots a THROWAWAY Centralaizer hub on an isolated temp data dir, runs the smoke against it,
# and tears everything down. The isolation is the whole point: Centralaizer's db/chroma/graph
# paths each default to ~/.localmem independently, so LM_DATA_DIR alone is NOT enough — all three
# LM_DB_PATH / LM_CHROMA_DIR / LM_GRAPH_PATH must point at the temp dir or the smoke writes to
# (and erases from) your real hub. This script sets all of them; do not run the smoke against a
# real hub by hand.
#
# Usage:
#   demo/run_smoke_forget_revoke.sh [CENTRALAIZER_DIR] [PORT]
# Env overrides:
#   CENTRALAIZER_DIR (default: ../Centralaizer)   CENTRALAIZER_PY (default: $CENTRALAIZER_DIR/.venv/bin/python)
#   AIMESSAGE_PY     (default: ./.venv/bin/python)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CENTRALAIZER_DIR="${1:-${CENTRALAIZER_DIR:-$HERE/../Centralaizer}}"
PORT="${2:-${PORT:-3015}}"
CENTRALAIZER_PY="${CENTRALAIZER_PY:-$CENTRALAIZER_DIR/.venv/bin/python}"
AIMESSAGE_PY="${AIMESSAGE_PY:-$HERE/.venv/bin/python}"

[ -x "$CENTRALAIZER_PY" ] || { echo "no Centralaizer python at $CENTRALAIZER_PY"; exit 2; }
[ -x "$AIMESSAGE_PY" ]    || { echo "no AIMessage python at $AIMESSAGE_PY"; exit 2; }

TMP="$(mktemp -d)"
cleanup() { [ -n "${UV_PID:-}" ] && kill "$UV_PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

# Isolate ALL store paths under the temp dir (see header).
export LM_DATA_DIR="$TMP" LM_DB_PATH="$TMP/memory.db" \
       LM_CHROMA_DIR="$TMP/chroma" LM_GRAPH_PATH="$TMP/graph.duckdb"

echo "temp hub data dir: $TMP"
( cd "$CENTRALAIZER_DIR" && "$CENTRALAIZER_PY" -c "from core.storage.database import init_db; init_db()" )
( cd "$CENTRALAIZER_DIR" && "$CENTRALAIZER_PY" -m uvicorn ui.app:app --host 127.0.0.1 --port "$PORT" --log-level warning ) >"$TMP/uv.log" 2>&1 &
UV_PID=$!

for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/api/stats" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "http://127.0.0.1:$PORT/api/stats" >/dev/null 2>&1 || { echo "hub failed to start:"; tail -20 "$TMP/uv.log"; exit 1; }

"$AIMESSAGE_PY" "$HERE/demo/smoke_forget_revoke.py" --base-url "http://127.0.0.1:$PORT"
