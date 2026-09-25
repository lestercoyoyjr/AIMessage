#!/usr/bin/env bash
# Run this on a SECOND machine on the SAME LAN as the AIMessage node daemon to join the mesh and
# query it — the real two-machine hop. No --lan: two distinct machine IPs satisfy the WAN eclipse
# guard, so the mesh forms without disabling protection.
#
#   ./machine2-join.sh                     # default demo query
#   ./machine2-join.sh "your question"     # custom query
#   BOOTSTRAP=/ip4/.../tcp/4210/p2p/<id> ./machine2-join.sh "q"   # override target
#
# The default BOOTSTRAP points at machine-1 as of the last deploy; if machine-1's LAN IP changed
# (DHCP) or it restarted, get the current one there with:  grep NODE_MULTIADDR= ~/.localmem/aimessage-node.log
# (and swap 0.0.0.0 for machine-1's LAN IP, `ipconfig getifaddr en0`).
set -euo pipefail

BOOTSTRAP="${BOOTSTRAP:-/ip4/192.168.0.43/tcp/4210/p2p/12D3KooWNaALnbVgPxtWRvuNd7FrZtJnfmP8n7U6sMD1HkxACUdQ}"
QUERY="${1:-which patients qualify for chronic care management?}"
DIR="${AIMESSAGE_DIR:-$HOME/dev/aimessage}"
REPO="${AIMESSAGE_REPO:-https://github.com/lestercoyoyjr/AIMessage_private.git}"

# derive host:port from the multiaddr for a fast reachability precheck
HOST="$(printf '%s' "$BOOTSTRAP" | sed -n 's#^/ip4/\([^/]*\)/tcp/\([0-9]*\).*#\1#p')"
PORT="$(printf '%s' "$BOOTSTRAP" | sed -n 's#^/ip4/\([^/]*\)/tcp/\([0-9]*\).*#\2#p')"

echo "== machine-2 join =="
echo "target: $HOST:$PORT"
if ! nc -G 3 -z "$HOST" "$PORT" 2>/dev/null; then
  echo "✗ can't reach $HOST:$PORT — check: same LAN? machine-1 daemon up? firewall allows Python inbound?"
  echo "  (on machine-1:  lsof -iTCP:$PORT -sTCP:LISTEN  and  ipconfig getifaddr en0)"
  exit 1
fi
echo "✓ $HOST:$PORT reachable"

command -v python3.12 >/dev/null || { echo "✗ need python3.12 (brew install python@3.12)"; exit 1; }
if [ ! -d "$DIR/.git" ]; then
  echo "cloning $REPO -> $DIR (needs your GitHub access to the private repo)…"
  git clone --branch develop "$REPO" "$DIR"
fi
cd "$DIR"
if [ ! -x .venv/bin/python ]; then
  echo "setting up venv + libp2p (first run only, a few minutes)…"
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -q -U pip
  .venv/bin/pip install --no-cache-dir -q ".[libp2p]"
fi

echo "asking the mesh… (WAN timings: settle 5s, window 8s)"
.venv/bin/python -m aimessage ask --bootstrap "$BOOTSTRAP" --settle 5 --window 8 "$QUERY"
echo
echo "If you see a verified answer above, the two-machine WAN hop works. 🎉"
