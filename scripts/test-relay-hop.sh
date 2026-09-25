#!/usr/bin/env bash
# Cross-internet RELAY test — proves two peers that CANNOT reach each other directly can still mesh
# through a publicly-reachable relay (the realistic answer to py-libp2p 0.7.0 having no NAT traversal:
# both NATed peers dial OUT to a common relay, which bridges their gossipsub traffic).
#
# Topology (two isolated Docker networks + a dual-homed relay = two NATed LANs + one public relay):
#
#     netA                         netB
#   [ peerA ] ──▶ [ relay ] ◀── [ peerB ]
#    serve         (both nets)      ask
#   peerA and peerB share NO network, so any answer B receives MUST have crossed the relay.
#
# Needs Docker + the aimessage:libp2p image (scripts/test-wan-hop.sh builds it).
set -euo pipefail
cd "$(dirname "$0")/.."

IMG="aimessage:libp2p"
NET_A="aimsg-relay-a"; NET_B="aimsg-relay-b"
QUERY="which patients qualify for chronic care management?"
TMP="$(mktemp -d)"

cleanup() {
  docker rm -f relay peerA peerB >/dev/null 2>&1 || true
  docker network rm "$NET_A" "$NET_B" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "✗ docker not running"; exit 2; }
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "✗ $IMG missing — run scripts/test-wan-hop.sh first"; exit 2; }

ip_on() { docker inspect -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" "$1"; }
peerid_of() { for _ in $(seq 1 45); do p="$(docker logs "$1" 2>&1 | sed -n 's#^NODE_MULTIADDR=.*/p2p/##p' | head -1)"; [ -n "$p" ] && { echo "$p"; return; }; sleep 1; done; }

cat > "$TMP/mems.json" <<'JSON'
[{"content":"Patients on a biologic with commercial insurance qualify for the chronic care management (CCM) billing program.","type":"semantic","owner":"federated","trust_score":0.9}]
JSON

docker network rm "$NET_A" "$NET_B" >/dev/null 2>&1 || true
docker network create "$NET_A" >/dev/null
docker network create "$NET_B" >/dev/null
echo "isolated networks $NET_A / $NET_B created"

# --- relay: empty-store node, dual-homed on both networks (the public relay) ---
echo "== relay (dual-homed, no store — just forwards gossipsub) =="
docker run -d --name relay --network "$NET_A" "$IMG" \
  aimessage serve --listen /ip4/0.0.0.0/tcp/4001 >/dev/null
docker network connect "$NET_B" relay
RELAY_PEER="$(peerid_of relay)"
[ -n "$RELAY_PEER" ] || { echo "✗ relay never advertised"; docker logs relay; exit 1; }
RELAY_A_IP="$(ip_on relay "$NET_A")"; RELAY_B_IP="$(ip_on relay "$NET_B")"
echo "  relay peer: $RELAY_PEER"
echo "  reachable at $RELAY_A_IP (netA) / $RELAY_B_IP (netB)"

# --- peerA: serves the memory, on netA ONLY, dials OUT to the relay ---
echo "== peerA (serve, netA only, bootstraps to relay) =="
docker run -d --name peerA --network "$NET_A" -v "$TMP/mems.json:/app/mems.json:ro" "$IMG" \
  aimessage serve --memories /app/mems.json --listen /ip4/0.0.0.0/tcp/4001 \
  --bootstrap "/ip4/$RELAY_A_IP/tcp/4001/p2p/$RELAY_PEER" >/dev/null
[ -n "$(peerid_of peerA)" ] || { echo "✗ peerA never came up"; docker logs peerA; exit 1; }
sleep 4   # let peerA graft onto the relay's mesh

# --- peerB: asks, on netB ONLY (cannot see peerA), dials OUT to the relay ---
echo "== peerB (ask, netB only — no path to peerA except through the relay) =="
OUT="$(docker run --rm --name peerB --network "$NET_B" "$IMG" \
  aimessage ask --bootstrap "/ip4/$RELAY_B_IP/tcp/4001/p2p/$RELAY_PEER" --settle 8 --window 12 "$QUERY" 2>&1)"
echo "---- peerB output ----"; echo "$OUT" | sed 's/^/  /'; echo "----------------------"

if echo "$OUT" | grep -qi "chronic care management" && echo "$OUT" | grep -q "verified=True"; then
  echo "✅ RELAY HOP VERIFIED — peerB got peerA's memory across two isolated networks via the relay."
else
  echo "✗ no relayed answer. relay + peerA logs:"; docker logs relay 2>&1 | tail -10; echo "--"; docker logs peerA 2>&1 | tail -10
  exit 1
fi
