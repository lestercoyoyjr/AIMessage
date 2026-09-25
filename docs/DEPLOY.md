# Deploying AIMessage for human testing

Local-first, so "deploy" = run a persistent node + wire the human-test setup. Machine 1 (this Mac)
runs an always-on node daemon that is Centralaizer-backed and acts as the mesh **bootstrap**; a real
MCP agent (Claude Desktop/Cursor) provides the write side; machine 2 joins the mesh to retrieve.

## Machine 1 — the node daemon (DONE on this Mac)
```bash
cd ~/dev/AIMessage_private
scripts/install-launchagent.sh          # com.aimessage.node — RunAtLoad + KeepAlive
```
Runs: `aimessage serve --centralaizer http://127.0.0.1:3001 --federate-shared --control
--control-port 4200 --writeback --notify --listen /ip4/0.0.0.0/tcp/4210` (no `--lan`: two real
machines have distinct IPs, which satisfies the WAN eclipse guards).

- **Dashboard**: `grep DASHBOARD= ~/.localmem/aimessage-node.log` → open the URL (token rotates each
  restart). "Ask the mesh" queries the mesh and absorbs answers back into the hub (Phase 7).
- **Bootstrap addr** for machine 2: `/ip4/<this-LAN-IP>/tcp/4210/p2p/<peer-id>`
  (`ipconfig getifaddr en0` + `grep node peer from NODE_MULTIADDR in the log`).
- Logs: `~/.localmem/aimessage-node.log` / `.err.log`. Manage: `launchctl {unload|load}` the plist;
  `scripts/install-launchagent.sh --uninstall`.

## Write side — connect a real MCP agent (on machine 1)
The agent writes to the **hub** (Centralaizer :3000), which the node daemon then federates.
```bash
cd ~/dev/Centralaizer && python connect_agents_cli.py     # interactive: backs up + asks per file
```
Then in Claude Desktop / Cursor, have the agent `memory_write` an `owner=shared` fact. Confirm it
landed: `curl 'http://127.0.0.1:3001/api/search?q=<terms>&owner=shared'`.

## Machine 2 — join the mesh and retrieve
**One command** (copy `scripts/machine2-join.sh` to the second machine, or clone the repo there):
```bash
./scripts/machine2-join.sh "which patients qualify for chronic care management?"
```
It reachability-checks machine-1, clones + sets up the venv on first run, and asks the mesh with
WAN-friendly timings. The bootstrap address is baked in from the last deploy; override with
`BOOTSTRAP=/ip4/<machine-1-LAN-IP>/tcp/4210/p2p/<peer-id> ./scripts/machine2-join.sh "…"`.

Manual equivalent:
```bash
git clone <repo> aimessage && cd aimessage && git checkout develop
python3.12 -m venv .venv && .venv/bin/pip install '.[libp2p]'
.venv/bin/python -m aimessage ask \
  --bootstrap /ip4/<machine-1-LAN-IP>/tcp/4210/p2p/<peer-id> \
  --settle 5 --window 8 "<the query>"          # no --lan: two distinct machine IPs mesh in WAN mode
```
Expect a signed, E2E-sealed answer from machine 1's federated hub memories. Get the current bootstrap
addr on machine 1 with `grep NODE_MULTIADDR= ~/.localmem/aimessage-node.log` (swap `0.0.0.0` for its
`ipconfig getifaddr en0`).

## WAN caveat (important)
py-libp2p 0.7.0 has **no NAT traversal**. This works out of the box only when both machines are on the
**same LAN**. Across the internet you must either port-forward TCP **4210** to machine 1 and use its
public IP in the bootstrap addr, or stand up a libp2p relay. A relay/AutoNAT story is future work
(Phase 6 / `docs/ROADMAP.md`).

## Teardown
`scripts/install-launchagent.sh --uninstall` (machine 1); the identity key persists at the default
key path so the peer id is stable across restarts.
