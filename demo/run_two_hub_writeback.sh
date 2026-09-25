#!/usr/bin/env bash
# Phase 7 two-hub proof: a memory Agent A writes into hub1 ends up searchable in Agent B's SEPARATE
# hub2, carried entirely over the gossip mesh — the real mesh→hub write-back scenario (not writing
# back into the same hub it came from).
#
#   Agent A --MCP write--> hub1 <--/api/search-- node1(--centralaizer hub1, in mesh)
#                                                    | gossipsub
#   node2(ask --writeback hub2) --mesh--> gets A's answer --> POST /api/memories --> hub2 (started EMPTY)
#   => hub2 /api/search now returns it, agent_id=mesh-writeback  (Agent B's own memory_search finds it)
#
# Two throwaway isolated hubs. hub1 = main.py (needs the MCP server for A's write); hub2 = uvicorn
# ui.app only (write-back target + search verify; single process, so no cross-proc chromadb issue).
#
# Usage:  demo/run_two_hub_writeback.sh [CENTRALAIZER_DIR]
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CENTRALAIZER_DIR="${1:-${CENTRALAIZER_DIR:-$HERE/../Centralaizer}}"
CPY="${CENTRALAIZER_PY:-$CENTRALAIZER_DIR/.venv/bin/python}"
APY="${AIMESSAGE_PY:-$HERE/.venv/bin/python}"
MCP1="${MCP1:-3060}"; UI1="${UI1:-3061}"; UI2="${UI2:-3071}"
QUERY="chronic care management biologic"
CONTENT="two-hub probe $$: patients on a biologic qualify for chronic care management billing"

[ -x "$CPY" ] || { echo "no Centralaizer python at $CPY"; exit 2; }
[ -x "$APY" ] || { echo "no AIMessage python at $APY"; exit 2; }

T1="$(mktemp -d)"; T2="$(mktemp -d)"; N1=""
cleanup() {
  [ -n "$N1" ] && kill "$N1" 2>/dev/null || true
  for port in "$MCP1" "$UI1" "$UI2"; do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | while read -r pid; do
      kill -9 "$pid" 2>/dev/null || true
    done
  done
  rm -rf "$T1" "$T2"
}
trap cleanup EXIT

step() { echo "  $1"; }
fail() { echo "  FAIL  $1"; exit 1; }
search_count() { curl -s "http://127.0.0.1:$1/api/search?q=$(printf '%s' "$QUERY" | tr ' ' '+')&n=5&owner=shared"; }

echo "two-hub write-back: hub1(main.py MCP :$MCP1/UI :$UI1)  hub2(ui.app :$UI2)"

# --- hub1: main.py (MCP + UI), Agent A's hub ---
( cd "$CENTRALAIZER_DIR" && env LM_DATA_DIR="$T1" LM_DB_PATH="$T1/memory.db" LM_CHROMA_DIR="$T1/chroma" \
    LM_GRAPH_PATH="$T1/graph.duckdb" LM_MCP_PORT="$MCP1" LM_UI_PORT="$UI1" \
    "$CPY" -c "from core.storage.database import init_db; init_db()" )
( cd "$CENTRALAIZER_DIR" && env LM_DATA_DIR="$T1" LM_DB_PATH="$T1/memory.db" LM_CHROMA_DIR="$T1/chroma" \
    LM_GRAPH_PATH="$T1/graph.duckdb" LM_MCP_PORT="$MCP1" LM_UI_PORT="$UI1" \
    "$CPY" main.py ) >"$T1/hub.log" 2>&1 &
for _ in $(seq 1 60); do curl -sf --max-time 2 "http://127.0.0.1:$UI1/api/stats" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf --max-time 2 "http://127.0.0.1:$UI1/api/stats" >/dev/null 2>&1 || { tail -20 "$T1/hub.log"; fail "hub1 UI down"; }
for _ in $(seq 1 40); do curl -s --max-time 2 -o /dev/null "http://127.0.0.1:$MCP1/mcp" 2>/dev/null && break; sleep 0.5; done

# --- hub2: ui.app only, Agent B's hub (starts EMPTY) ---
( cd "$CENTRALAIZER_DIR" && env LM_DATA_DIR="$T2" LM_DB_PATH="$T2/memory.db" LM_CHROMA_DIR="$T2/chroma" \
    LM_GRAPH_PATH="$T2/graph.duckdb" LM_UI_PORT="$UI2" \
    "$CPY" -c "from core.storage.database import init_db; init_db()" )
( cd "$CENTRALAIZER_DIR" && env LM_DATA_DIR="$T2" LM_DB_PATH="$T2/memory.db" LM_CHROMA_DIR="$T2/chroma" \
    LM_GRAPH_PATH="$T2/graph.duckdb" LM_UI_PORT="$UI2" \
    "$CPY" -m uvicorn ui.app:app --host 127.0.0.1 --port "$UI2" --log-level warning ) >"$T2/hub.log" 2>&1 &
for _ in $(seq 1 60); do curl -sf --max-time 2 "http://127.0.0.1:$UI2/api/stats" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf --max-time 2 "http://127.0.0.1:$UI2/api/stats" >/dev/null 2>&1 || { tail -20 "$T2/hub.log"; fail "hub2 UI down"; }
step "PASS  both hubs up"

# --- Agent A writes into hub1 via MCP; hub2 must be empty for the query ---
W="$("$CPY" "$HERE/demo/_agent_write.py" "http://127.0.0.1:$MCP1/mcp" "$CONTENT")"
echo "$W" | grep -q '"status": *"stored"' || { echo "  $W"; fail "MCP write to hub1 did not store"; }
echo "$(search_count "$UI2")" | grep -q '"content"' && fail "hub2 unexpectedly already has the memory"
step "PASS  Agent A wrote to hub1 via MCP; hub2 is empty"

# --- node1 federates hub1 into the mesh ---
( "$APY" -m aimessage serve --centralaizer "http://127.0.0.1:$UI1" --federate-shared --lan \
    --listen /ip4/127.0.0.1/tcp/0 ) >"$T1/node1.log" 2>&1 &
N1=$!
ADDR=""
for _ in $(seq 1 60); do ADDR="$(grep -m1 '^NODE_MULTIADDR=' "$T1/node1.log" 2>/dev/null | cut -d= -f2- || true)"; [ -n "$ADDR" ] && break; sleep 0.5; done
[ -n "$ADDR" ] || { tail -20 "$T1/node1.log"; fail "node1 never advertised a multiaddr"; }
step "PASS  node1 federating hub1 at $ADDR"

# --- node2 (Agent B) asks the mesh and writes the answer into hub2 ---
ANS="$("$APY" -m aimessage ask --bootstrap "$ADDR" --lan --settle 3 --window 6 \
        --writeback "http://127.0.0.1:$UI2" "$QUERY")"
echo "---- node2 (Agent B) ----"; echo "$ANS"; echo "-------------------------"
echo "$ANS" | grep -qi "chronic care management" || fail "node2 got no mesh answer"

# --- the payoff: hub2 (Agent B's own hub) now returns A's memory, tagged mesh-writeback ---
sleep 1
H2="$(search_count "$UI2")"
echo "$H2" | grep -qi "chronic care management" || { echo "  hub2: $H2"; fail "hub2 did not absorb the answer"; }
echo "$H2" | grep -q '"agent_id": *"mesh-writeback"' || { echo "  hub2: $H2"; fail "absorbed memory not tagged mesh-writeback"; }
step "PASS  Agent B's hub2 now returns A's memory (agent_id=mesh-writeback)"
echo ""
echo "TWO-HUB WRITE-BACK GREEN — A's memory crossed the mesh into B's separate hub."
