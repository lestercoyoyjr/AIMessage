# AIMessage — testing checklist

What's automated vs. what still needs a human, a second host, or an external account. Automated tests
run via `for f in tests/test_*.py; do python "$f"; done` (or pytest); the libp2p/gossip/dht/wire tests
self-skip without the `[libp2p]` extra and run in CI's `test-libp2p` job.

## Automated (in the suite / CI)
- [x] Unit + integration + regression + edge + sanitization — 145 tests, CI `test` + `test-libp2p` + `docker`.
- [x] **forget→revoke HTTP chain** — `demo/run_smoke_forget_revoke.sh` (live, against a throwaway isolated hub).
- [x] **forget→revoke mesh WIRE hop** — `tests/test_revoke_wire.py` (2-node tombstone propagation + authorship-bound drop). Runs in CI `test-libp2p`.
- [x] **semantic-tracing forget** (Centralaizer repo) — `tests/integration/test_semantic_forget.py`. Needs real Ollama embeddings; self-skips otherwise (so CI skips it — run locally).
- [x] **desktop sidecar handshake** — `python desktop/app.py --selfcheck` (headless).

## Needs a human / a display (can't be headless)
- [ ] **Desktop window itself.** `pip install '.[desktop]'` → `python desktop/app.py`. Click through: status, storage meter, event feed, "Ask the mesh". (Only the sidecar handshake is auto-tested; `webview.start()` is not.)
- [ ] **Cross-platform desktop** — repeat the above on Linux (WebKitGTK) and Windows (WebView2); only macOS/WKWebView is reasoned about.
- [x] **The real product loop** — agent→hub→mesh→agent. **Verified live** via `demo/run_product_loop.sh`: real MCP `memory_write` → hub → `node1 --centralaizer` federates → gossip mesh → `node2` gets the verified, E2E-sealed answer. Manual real-agent (Claude Desktop/Cursor) steps in `docs/PRODUCT_LOOP.md`. Two fixes it took: node1 must `--listen` loopback (not the 0.0.0.0 default) to be dialable; and Centralaizer `/api/search` had to degrade to FTS5 instead of 500-ing when chromadb can't share a vector segment across `main.py`'s MCP-writer / UI-reader processes. Known boundary (now Phase 7 in `docs/ROADMAP.md`): the retrieving end is a *node* (`ask`/dashboard); auto-writing a mesh answer back into a second agent's hub isn't built.

## Needs a second real host / a network
- [x] **Two real machines (same LAN)** — VERIFIED 2026-09-23: machine-1 runs the deployed node daemon (bootstrap on `:4210`), a second physical machine on the same LAN ran `scripts/machine2-join.sh` and got a signed, `verified=True` answer over the mesh. Two distinct machine IPs satisfy the WAN eclipse guard (no `--lan`). Reproducible via `scripts/machine2-join.sh`.
- [x] **Reproducible WAN hop (no second box)** — `scripts/test-wan-hop.sh`: two AIMessage nodes as Docker containers on a bridge network get **distinct IPs**, so they mesh in WAN mode (no `--lan`) and node B retrieves node A's federated memory `verified=True`. Same condition as two physical machines, runnable anywhere Docker is. (Also fixed the Dockerfile's `[libp2p]` build — it was missing `libgmp-dev` for `fastecdsa`.)
- [~] **Cross-internet via relay** — mechanism **PROVEN reproducibly**: `scripts/test-relay-hop.sh` bridges two *isolated* Docker networks with a dual-homed relay (peers share no network) and peer B still retrieves peer A's memory `verified=True` — i.e. two NATed peers meshing through one public relay. Deploy guide + trust model in `docs/RELAY.md`. Remaining is ops, not code: stand a relay on a real public host (VPS/port-forward). Direct NAT-traversal without a relay (AutoNAT/Circuit-Relay-v2) is still absent in py-libp2p 0.7.0.
- [ ] **DHT provider lookup at scale** — py-libp2p 0.7.0 `find_providers` returns only self on tiny nets; today reachability leans on the bootstrap relay. Prove real multi-peer discovery.

## Needs an external account
- [ ] **Google Drive cold-tier** — `aimessage/gdrive.py` real client is untested; needs your OAuth (link-only, never auto-purchase).
- [ ] **A2A interop** — the Agent Card at `/.well-known/agent-card.json` is published, but no A2A-native agent has discovered/used it.

## Not yet built (would be automatable)
- [x] **Load / soak** — `tests/test_load_soak.py` (8 tests): 40k-query firehose keeps `ReplayGuard._seen` ≤ cap and fails **closed** (never OOMs); advancing-clock stream self-heals (prunes) and is never wrongly rejected; replays never re-accepted under load; `TombstoneLog` + `ArtifactCache` stay bounded under 50×/500× churn; `rank_corroborated` collapses a 20k-answer firehose to distinct content; `handle_query` end-to-end at 3k; `tracemalloc` soak proves flat peak memory. Pure/deterministic (controlled clock), CI-safe.
