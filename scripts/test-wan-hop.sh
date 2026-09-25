#!/usr/bin/env bash
# Reproducible two-"machine" WAN-hop test — no second physical box needed.
#
# Runs two AIMessage nodes as separate Docker containers on a bridge network. Each container gets a
# DISTINCT IP, which is exactly what the WAN gossipsub eclipse guard needs (it rejects only same-IP
# peers) — so the mesh forms WITHOUT --lan, the same condition as two real machines. Node A serves a
# federated memory; node B, from a different container IP, asks the mesh and must get it back verified.
#
#   scripts/test-wan-hop.sh
#
# Needs Docker running. First run builds the libp2p image (heavy: trio/grpcio/aioquic).
set -euo pipefail
cd "$(dirname "$0")/.."

IMG="aimessage:libp2p"
NET="aimsg-wan-test"
QUERY="which patients qualify for chronic care management?"
TMP="$(mktemp -d)"

cleanup() {
  docker rm -f aimsg-a aimsg-b >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "✗ docker not installed"; exit 2; }
docker info >/dev/null 2>&1 || { echo "✗ docker daemon not running — start Docker Desktop"; exit 2; }

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "building $IMG (first run only — heavy: build-essential + trio/grpcio/aioquic)…"
  docker build --build-arg WITH_LIBP2P=1 -t "$IMG" .
fi

cat > "$TMP/mems.json" <<'JSON'
[{"content":"Patients on a biologic with commercial insurance qualify for the chronic care management (CCM) billing program.","type":"semantic","owner":"federated","trust_score":0.9}]
JSON

docker network rm "$NET" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null
echo "network $NET created"

echo "== node A (responder, WAN mode, no --lan) =="
docker run -d --name aimsg-a --network "$NET" -v "$TMP/mems.json:/app/mems.json:ro" "$IMG" \
  aimessage serve --memories /app/mems.json --listen /ip4/0.0.0.0/tcp/4001 >/dev/null

PEER=""
for _ in $(seq 1 45); do
  PEER="$(docker logs aimsg-a 2>&1 | sed -n 's#^NODE_MULTIADDR=.*/p2p/##p' | head -1)"
  [ -n "$PEER" ] && break; sleep 1
done
[ -n "$PEER" ] || { echo "✗ node A never advertised a multiaddr:"; docker logs aimsg-a; exit 1; }
# `index` (not dot-notation) — the network name has hyphens, which Go templates can't field-access.
A_IP="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" aimsg-a)"
BOOT="/ip4/$A_IP/tcp/4001/p2p/$PEER"
echo "  A container IP: $A_IP"
echo "  bootstrap:      $BOOT"

echo "== node B (asker, on a DIFFERENT container IP — bridge assigns one per container) =="
OUT="$(docker run --rm --name aimsg-b --network "$NET" "$IMG" \
  aimessage ask --bootstrap "$BOOT" --settle 5 --window 8 "$QUERY" 2>&1)"
echo "---- node B output ----"; echo "$OUT" | sed 's/^/  /'; echo "-----------------------"

if echo "$OUT" | grep -qi "chronic care management" && echo "$OUT" | grep -q "verified=True"; then
  echo "✅ TWO-CONTAINER WAN HOP VERIFIED — distinct-IP nodes meshed in WAN mode and returned a signed answer."
else
  echo "✗ no verified answer — see node A logs:"; docker logs aimsg-a 2>&1 | tail -20
  exit 1
fi
