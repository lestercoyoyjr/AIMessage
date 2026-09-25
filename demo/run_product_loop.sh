#!/usr/bin/env bash
# The real product loop, end-to-end and automated: an MCP agent writes a memory to Centralaizer, and a
# DIFFERENT AIMessage node retrieves it across the gossip mesh. Proves agent -> hub -> mesh -> agent.
#
#   Agent A  --MCP memory_write-->  Centralaizer hub  <--/api/search--  node1 (--centralaizer, in mesh)
#                                                                          |  gossipsub
#   node2  --broadcast_ask-->  mesh  -->  node1 seals A's memory back  -->  node2   (= Agent B retrieves)
#
# Everything runs on a THROWAWAY isolated hub (all four store paths + MCP/UI ports redirected), so it
# never touches your real ~/.localmem. Needs Centralaizer's stack (chromadb/etc.) and AIMessage's
# libp2p extra installed.
#
# Usage:  demo/run_product_loop.sh [CENTRALAIZER_DIR]
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CENTRALAIZER_DIR="${1:-${CENTRALAIZER_DIR:-$HERE/../Centralaizer}}"
CPY="${CENTRALAIZER_PY:-$CENTRALAIZER_DIR/.venv/bin/python}"
APY="${AIMESSAGE_PY:-$HERE/.venv/bin/python}"
MCP_PORT="${MCP_PORT:-3020}"; UI_PORT="${UI_PORT:-3021}"
QUERY="chronic care management biologic"
CONTENT="product-loop probe $$: patients on a biologic qualify for chronic care management billing"

[ -x "$CPY" ] || { echo "no Centralaizer python at $CPY"; exit 2; }
[ -x "$APY" ] || { echo "no AIMessage python at $APY"; exit 2; }

TMP="$(mktemp -d)"
HUB=""; N1=""
cleanup() {
  [ -n "$N1" ]  && kill "$N1"  2>/dev/null || true
  [ -n "$HUB" ] && kill "$HUB" 2>/dev/null || true
  # main.py spawns a UI uvicorn CHILD that outlives `kill $HUB`; free our two ports precisely (scoped
  # to this run's MCP_PORT/UI_PORT, so a real hub on other ports is never touched).
  for port in "$MCP_PORT" "$UI_PORT"; do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | while read -r pid; do
      kill -9 "$pid" 2>/dev/null || true
    done
  done
  rm -rf "$TMP"
}
trap cleanup EXIT

# Isolate EVERY store + both service ports (LM_DATA_DIR alone does NOT isolate — see the smoke runner).
export LM_DATA_DIR="$TMP" LM_DB_PATH="$TMP/memory.db" \
       LM_CHROMA_DIR="$TMP/chroma" LM_GRAPH_PATH="$TMP/graph.duckdb" \
       LM_MCP_PORT="$MCP_PORT" LM_UI_PORT="$UI_PORT"

step() { echo "  $1"; }
fail() { echo "  FAIL  $1"; exit 1; }

echo "product loop on throwaway hub: $TMP  (MCP :$MCP_PORT, UI :$UI_PORT)"
( cd "$CENTRALAIZER_DIR" && "$CPY" -c "from core.storage.database import init_db; init_db()" )
( cd "$CENTRALAIZER_DIR" && "$CPY" main.py ) >"$TMP/hub.log" 2>&1 &
HUB=$!

# hub readiness: UI /api/stats up AND the MCP port accepting connections (bounded curls so a
# non-responsive port can't hang the loop).
for _ in $(seq 1 60); do curl -sf --max-time 2 "http://127.0.0.1:$UI_PORT/api/stats" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf --max-time 2 "http://127.0.0.1:$UI_PORT/api/stats" >/dev/null 2>&1 || { tail -30 "$TMP/hub.log"; fail "hub UI never came up"; }
for _ in $(seq 1 40); do curl -s --max-time 2 -o /dev/null "http://127.0.0.1:$MCP_PORT/mcp" 2>/dev/null && break; sleep 0.5; done
step "PASS  hub up (UI + MCP)"

# 1. Agent A writes through the MCP server (real streamable-http tool call).
WRITE="$("$CPY" "$HERE/demo/_agent_write.py" "http://127.0.0.1:$MCP_PORT/mcp" "$CONTENT")"
echo "$WRITE" | grep -q '"status": *"stored"' || { echo "  write result: $WRITE"; fail "MCP memory_write did not store (quarantined/merged?)"; }
step "PASS  Agent A wrote a shared memory via MCP ($WRITE)"

# 2. node1 = a Centralaizer-backed gossip node joins the mesh (federates owner=shared).
# --listen loopback: the default binds 0.0.0.0, which advertises an undialable wildcard addr for node2.
( "$APY" -m aimessage serve --centralaizer "http://127.0.0.1:$UI_PORT" --federate-shared --lan \
    --listen /ip4/127.0.0.1/tcp/0 ) >"$TMP/node1.log" 2>&1 &
N1=$!
ADDR=""
for _ in $(seq 1 60); do ADDR="$(grep -m1 '^NODE_MULTIADDR=' "$TMP/node1.log" 2>/dev/null | cut -d= -f2- || true)"; [ -n "$ADDR" ] && break; sleep 0.5; done
[ -n "$ADDR" ] || { tail -30 "$TMP/node1.log"; fail "node1 never advertised a multiaddr"; }
step "PASS  node1 (Centralaizer-backed) in the mesh at $ADDR"

# 3. node2 = Agent B's node broadcasts the query across the mesh and retrieves the answer.
ANS="$("$APY" -m aimessage ask --bootstrap "$ADDR" --lan --settle 3 --window 6 "$QUERY")"
echo "---- node2 (Agent B) answers ----"; echo "$ANS"; echo "---------------------------------"
echo "$ANS" | grep -qi "chronic care management" \
  && { step "PASS  Agent B retrieved A's memory across the mesh"; echo; echo "PRODUCT LOOP GREEN — agent -> hub -> mesh -> agent."; } \
  || fail "Agent B did not receive A's memory over the mesh"
