# The real product loop — agent → hub → mesh → agent

The end-to-end path the whole system exists for: one AI agent records something, and a *different*
agent — on another node — gets it back across the mesh, de-identified and E2E-sealed.

```
Agent A ──MCP memory_write──▶ Centralaizer hub ◀──/api/search── node1 (--centralaizer, in the mesh)
                                                                    │ gossipsub
node2 ──broadcast_ask──▶ mesh ──▶ node1 seals A's memory back ──▶ node2  ( = Agent B retrieves )
```

## Current boundary (be honest about it)
Today the **retrieving** end is an AIMessage node (`aimessage ask` / `broadcast_ask` / the dashboard's
"Ask the mesh"), which surfaces the answer to a person or a caller. Auto-**writing** a mesh answer back
into a second agent's Centralaizer hub (so Agent B's own `memory_search` returns it without asking the
mesh) is **not built yet** — it's now tracked as **Phase 7 (mesh→hub write-back)** in `docs/ROADMAP.md`,
mirroring the forgotten-poller that already runs the other direction. So "Agent B retrieves" = Agent B's
*node* retrieves; the last hop into Agent B's hub is manual/CLI for now.

## Automated verification
```bash
demo/run_product_loop.sh            # boots a throwaway isolated hub, does it all, asserts, tears down
```
Proves: MCP `memory_write` → stored on the hub → `node1` federates `owner=shared` → `node2` broadcasts
over the gossip mesh → node1 seals A's memory back → node2 receives it (verified + decrypted).
Retrieval matches on FTS5 tokens, so it does **not** need Ollama. Needs Centralaizer's stack and
AIMessage's `[libp2p]` extra installed.

## Manual, with a real MCP agent (Claude Desktop / Cursor)
1. **Start a hub:** `cd ../Centralaizer && .venv/bin/python main.py`  (MCP :3000, UI :3001)
2. **Connect your agent** to it: `python connect_agents_cli.py`  (writes the MCP server entry into
   Claude Desktop / Cursor config; backs up first, merges, idempotent). Restart the agent.
3. **Agent A writes** — in Claude Desktop/Cursor, have the agent call `memory_write` (e.g. ask it to
   "remember that patients on a biologic qualify for chronic care management billing", `owner="shared"`).
   Confirm it landed: open the UI at http://localhost:3001 or `curl 'http://localhost:3001/api/search?q=chronic+care&owner=shared'`.
4. **node1 joins the mesh, backed by that hub:**
   ```bash
   python -m aimessage serve --centralaizer http://127.0.0.1:3001 --federate-shared --lan
   # note the printed NODE_MULTIADDR=...
   ```
5. **Agent B's node retrieves across the mesh:**
   ```bash
   python -m aimessage ask --bootstrap <NODE_MULTIADDR> --lan "chronic care management biologic"
   ```
   You should see A's memory come back — signed by the responder, sealed to the asker, PII-masked
   upstream. That's the loop.

For a true **two-machine** run, do steps 1–4 on machine 1 and step 5 on machine 2 (drop `--lan`, and
make node1 reachable — see the NAT/relay note in `docs/TESTING_CHECKLIST.md`).
