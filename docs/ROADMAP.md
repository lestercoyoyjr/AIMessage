# AIMessage — Roadmap

Seven phases, each shippable on its own. **Critical path to a demo: Phase 1 → 2 → 3** (two laptops,
one asks "have we solved X?", gets a signed, masked, E2E-encrypted answer from the other).

Guiding rule: don't reinvent P2P. Take **libp2p** (identity + Noise transport + gossipsub +
Kademlia DHT + NAT traversal). Building your own overlay is the 3am-pager.

> Note on the runtime: `py-libp2p` is less mature than go/rust/js. Start with it for known-peer
> gossipsub; if it bites, run a small Go libp2p sidecar the Python hub talks to over localhost
> (same pattern Centralaizer already uses for its UI subprocess).

---

## Phase 0 — Repo & license hygiene
- [x] Apache-2.0 `LICENSE`
- [ ] `NOTICE` + dependency license inventory (see README)
- Boundary: AIMessage is its own repo; depends on Centralaizer as the storage/trust/masking layer.

## Phase 1 — Make a memory portable (the prerequisite everyone skips)
A hub can't share a memory until a memory is a self-contained, signed object.
- [ ] Portable memory format: signed JSON `{content(masked), type, provenance, trust_score, created, origin_node, sig}`
- [ ] Fix the embed fallback so a peer's query doesn't 500 when Ollama is down (Centralaizer `vector_store.py`)
- [ ] Add owner tier `personal | shared | federated`; only `federated` ever leaves the box
- **Papers:** Portable Agent Memory (2605.11032), MemOS (2507.03724), Memanto (2604.22085)

## Phase 2 — Identity & contacts
- [ ] Keypair per hub on first run (PyNaCl) → PeerID = the node's "phone number"
- [ ] Manual peering: exchange `{peer_id, addr}` out of band (no directory / DHT yet)
- [ ] Extend the Bayesian trust prior to remote PeerIDs — new peer starts low-trust
- **Papers:** ANP white paper (2508.00007), Collaborative Memory (2505.18279)

## Phase 3 — The channel (the WhatsApp part)
- [ ] libp2p node in-process: Noise transport + one gossipsub topic per network/group
- [ ] The one primitive: signed query → peers run `memory_search` over `federated` → mask → seal reply to asker's pubkey → asker's trust engine ranks
- [ ] Borrow A2A message/task JSON for wire semantics (Apache-2.0, embeddable)
- **Papers:** Internet of Agents (2407.07061), Agora (2410.11905), Agent Interop Survey (2505.02279), Comms Security Survey (2506.19676)

## Phase 4 — Artifacts: plugins & MCP servers (the BitTorrent part)
- [ ] Content-address every artifact (hash = ID = integrity check)
- [ ] Advertise `{hash, manifest, sig}` on the topic; fetch from any holder; verify hash + sig on arrival
- [ ] **Hard human gate**: install requires reviewing the signed manifest and clicking yes. Never auto-install, never auto-run.
- `# ponytail: sig + human gate only; sandbox (container/WASM) before opening to strangers`
- **Papers:** MCP security landscape (2503.23278), Mesh Memory Protocol (2604.19540)

## Phase 5 — Network trust & erasure (the differentiator)
- [ ] Peer reputation updates from answer quality (reuse the quarantine approve/reject loop)
- [ ] GDPR-grade erasure across the mesh: signed **tombstones** ("origin revokes hash X") propagated on the topic, honored by compliant nodes, logged in `forgotten.propagated` with ack tracking
- Reality: you can't force deletion on a hostile peer (open problem) — but you can prove you requested it and track who acked. Paper-worthy.
- **Papers:** Mnemonic Sovereignty (2604.16548), Governed Collaborative Memory (2605.04264), Multi-Agent Memory: Computer-Architecture Perspective (2603.10062)

## Phase 6 — Only when real strangers join (YAGNI until then)
- DHT open discovery, plugin sandboxing (WASM/container), group topics with membership crypto, relays.
- Trigger is external (peers you don't personally trust), not calendar-based.

## Phase 7 — Close the loop: mesh → hub write-back (added 2026-09-09; ask-triggered path SHIPPED)
The product loop (agent → hub → mesh → agent) was wired *except the last hop*: a mesh answer surfaced
at a **node** (`aimessage ask` / dashboard "Ask the mesh"), not in the asking agent's own Centralaizer
hub — so Agent B's `memory_search` didn't return it until a human relayed it. This phase writes accepted
mesh answers back into the local hub, so retrieval is transparent to the agent.
- [x] **Opt-in ask-triggered write-back** (`aimessage/writeback.py` + `ask --writeback HUB_URL`):
      `broadcast_ask`'s already-ranked answers are POSTed to the local Centralaizer `POST /api/memories`
      (`owner=shared`), tagged `metadata.source=mesh` + `origin_node` + `content_address` so they're
      auditable, never look locally authored, and a later tombstone can match. Verified live: a written-
      back answer is immediately found by `/api/search`. 5 unit tests (injected `_post` seam).
- [x] Trust gate the write: mesh-side = `broadcast_ask` corroboration/`trusted_origins` ranking (upstream);
      hub-side = Centralaizer's own trust gate + PII mask on `/api/memories` (low-trust → quarantine, not
      the live store). Two independent gates.
- [x] Idempotent + tombstone-aware: dedup is hub-side (`write_memory` merges on cosine); write-back skips
      any answer this node has locally revoked (`is_revoked(content_address)`). *One-hop:* it checks the
      node's tombstone log, not the hub's `forgotten` table — cross-check is a refinement.
- [x] Freshness passthrough: `CentralaizerStore` carries the memory's **volatility class** in signed
      provenance (query-invariant → content-address stays stable; NOT time-dependent stale/age), and
      write-back sends it as `metadata.volatility` so the hub keeps it (`setdefault` → our value wins)
      instead of re-inferring. Staleness itself is still recomputed hub-side from volatility + age.
- [x] Dashboard: write-back wired into the control `POST /ask` (`serve --writeback` → the "Ask the mesh"
      box shows "absorbed N answer(s) into your hub"); best-effort (a hub hiccup never fails the ask).
- [x] Two-hub demo: `demo/run_two_hub_writeback.sh` — Agent A writes to hub1 via MCP, and it crosses the
      mesh into Agent B's **separate empty hub2** (tagged `agent_id=mesh-writeback`). Verified green.
- Mirror of the existing **forgotten-poller** (which runs hub → mesh for erasure); this is the
  mesh → hub direction. Reuses the CentralaizerStore seam + the corroboration ranker already built.
- **Phase 7 COMPLETE** (2026-09-09). Only YAGNI-by-design leftover: an always-on subscribe-and-absorb
  firehose (vs today's ask-triggered) — build when a real use case needs it. One refinement noted above:
  tombstone cross-check is one-hop (node log, not the hub `forgotten` table).
- **Why not sooner:** writing peer-sourced content into your hub is a real trust boundary — it waits
  on the trust anchor (done, v0.4.3) and the corroboration ranker (done) so the gate has teeth.
- `# ponytail: start with ask-triggered write-back (human/agent asked → answer is wanted); a always-on`
  `# subscribe-and-absorb firehose is a separate, later opt-in — don't build it until someone needs it.`
- **Papers:** Governed Collaborative Memory (2605.04264), Collaborative Memory / multi-user sharing
  (2505.18279), Mnemonic Sovereignty (2604.16548).
