#!/usr/bin/env bash
#
# Install a macOS LaunchAgent so an AIMessage node auto-starts on login and auto-restarts if it
# exits (KeepAlive). The node is Centralaizer-backed and acts as a mesh BOOTSTRAP on a fixed port
# so a second machine can join it (--bootstrap /ip4/<this-LAN-IP>/tcp/$P2P_PORT/p2p/<peer-id>).
#
# Config (env overrides): HUB_URL, P2P_PORT, CONTROL_PORT.
# Usage:  scripts/install-launchagent.sh            # install + load
#         scripts/install-launchagent.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
LABEL="com.aimessage.node"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.localmem"
HUB_URL="${HUB_URL:-http://127.0.0.1:3001}"
P2P_PORT="${P2P_PORT:-4210}"          # fixed so machine-2 can bootstrap to it
CONTROL_PORT="${CONTROL_PORT:-4200}"  # fixed dashboard port (token still rotates per launch)

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

[ -x "$PY" ] || { echo "venv python not found at $PY — create the venv first" >&2; exit 1; }
"$PY" -c "import libp2p" 2>/dev/null || { echo "libp2p missing — pip install '.[libp2p]'" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

# No --lan: two distinct machines = two IPs, which satisfies the WAN eclipse/spam guards. Bind all
# interfaces on a fixed port (advertised addr says 0.0.0.0; machine-2 dials this host's real LAN IP).
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>aimessage</string><string>serve</string>
    <string>--centralaizer</string><string>$HUB_URL</string>
    <string>--federate-shared</string>
    <string>--control</string><string>--control-port</string><string>$CONTROL_PORT</string>
    <string>--writeback</string>
    <string>--notify</string>
    <string>--listen</string><string>/ip4/0.0.0.0/tcp/$P2P_PORT</string>
    <string>--name</string><string>node-$(hostname -s)</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/aimessage-node.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/aimessage-node.err.log</string>
</dict>
</plist>
EOF

# free the p2p port from any manual instance, then (re)load. (|| true: lsof exits 1 when the port is
# free, which would trip set -o pipefail.)
{ lsof -nP -iTCP:"$P2P_PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | while read -r pid; do kill "$pid" 2>/dev/null || true; done; } || true
sleep 1
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
echo "installed + loaded $LABEL"
echo "  hub:        $HUB_URL"
echo "  p2p port:   $P2P_PORT   control/dashboard port: $CONTROL_PORT"
echo "  logs:       $LOGDIR/aimessage-node.log  (errors: aimessage-node.err.log)"
echo "  dashboard:  grep DASHBOARD= $LOGDIR/aimessage-node.log   (token rotates per launch)"
echo "  peer id:    grep node_id= $LOGDIR/aimessage-node.log"
echo "  manage:     launchctl {unload|load} \"$PLIST\"   ·   uninstall: $0 --uninstall"
