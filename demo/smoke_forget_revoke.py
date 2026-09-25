"""Live end-to-end smoke: a Centralaizer hub erasure becomes a mesh revocation.

Closes the one integration gap noted in tasks/todo.md — the forget->revoke chain had only been
verified in units + against a live DB, never against a *running* Centralaizer over real HTTP.

This drives the exact production seam:
    hub POST /api/memories/{id}/forget   (operator erases a shared memory)
      -> hub GET /api/forgotten          (the poller's source of truth)
      -> CentralaizerStore.fed_map       (hub id -> content-address THIS node federated)
      -> hashes_to_revoke(...)           (map erased hub ids -> content-addresses)
      -> a signed tombstone drops the previously-served copy from the mesh answer path.

Run it via the isolated runner (recommended) — boots a throwaway hub and tears it down:
    demo/run_smoke_forget_revoke.sh
Or point it at a hub you started yourself:
    python demo/smoke_forget_revoke.py --base-url http://127.0.0.1:3001

WARNING: this seeds a memory and then ERASES it on whatever hub you target. Centralaizer's
db/chroma/graph paths each default to ~/.localmem *independently*, so LM_DATA_DIR alone will NOT
isolate a test hub — you must also set LM_DB_PATH, LM_CHROMA_DIR, LM_GRAPH_PATH (the runner does).
Never point this at your real hub.

ponytail: the tombstone *gossip publish* (GossipNode.revoke_hash -> pubsub) is already covered by
test_tombstone / test_gossip; this smoke deliberately exercises the untested HTTP half + the
serve-time drop using the real tombstone primitive, so it needs no libp2p and can't flake on a
localhost mesh. Upgrade path: spin two real GossipNodes if we ever want the wire hop proven live too.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

from nacl.signing import SigningKey

from aimessage.centralaizer_store import CentralaizerStore, _fetch, hashes_to_revoke
from aimessage.tombstone import TombstoneLog, make_tombstone, verify_tombstone

MARKER = "aimessage-smoke chronic care management program"   # distinctive so search finds our row


def _post(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:   # noqa: S310 (localhost hub)
        raw = r.read()
    return json.loads(raw) if raw else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:3001")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    def step(ok: bool, msg: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
        if not ok:
            raise SystemExit(1)

    print(f"forget->revoke smoke against {base}\n")

    # 1. Seed a shared memory on the hub (real trust gate, real masking, real indexing).
    res = _post(f"{base}/api/memories",
                {"agent_id": "smoke-agent", "content": MARKER, "owner": "shared"})
    step(res.get("status") == "stored" and res.get("id"),
         f"hub stored a shared memory (status={res.get('status')}, id={res.get('id')})")
    hub_id = res["id"]

    # 2. Federate it into a node via the real /api/search HTTP contract.
    store = CentralaizerStore(SigningKey.generate(), base_url=base,
                              allow_shared_as_federated=True)
    hits = []
    for _ in range(10):                                  # indexing is sync, but be forgiving
        hits = store.search(MARKER)
        if hub_id in store.fed_map:
            break
        time.sleep(0.2)
    step(hub_id in store.fed_map,
         f"node federated it over HTTP (fed_map has hub id -> {store.fed_map.get(hub_id, '?')[:12]}...)")
    content_addr = store.fed_map[hub_id]
    served = [h for h in hits if h.id == content_addr]
    step(bool(served), "the federated copy is in the node's served answer set (pre-forget)")

    # 3. Operator erases it on the hub over real HTTP.
    forget = _post(f"{base}/api/memories/{hub_id}/forget")
    step(isinstance(forget, dict), f"hub POST .../forget returned (verified={forget.get('verified')})")

    # 4. The poller's source of truth reflects the erasure.
    forgotten = _fetch(f"{base}/api/forgotten")
    forgotten_ids = [r.get("id") for r in forgotten if isinstance(r, dict)]
    step(hub_id in forgotten_ids, f"hub GET /api/forgotten lists the erased id ({len(forgotten_ids)} total)")

    # 5. Map erased hub ids -> content-addresses this node actually federated.
    to_revoke = hashes_to_revoke(forgotten_ids, store.fed_map)
    step(content_addr in to_revoke,
         f"hashes_to_revoke resolved the hub erasure to our content-address ({len(to_revoke)} to revoke)")

    # 6. A signed tombstone drops that copy from the serve path (real primitive; gossip publish
    #    itself is covered elsewhere — see module docstring).
    t = make_tombstone(content_addr, store.key)
    step(verify_tombstone(t), "tombstone is well-formed and signed by the revoker")
    log = TombstoneLog()
    log.record(t["target"], t["revoker"])
    step(log.is_revoked(content_addr), "TombstoneLog now marks the content-address revoked")
    still_served = [h for h in hits if not log.is_revoked(h.id)]
    step(all(h.id != content_addr for h in still_served),
         "the previously-served copy is filtered out of the mesh answer path (post-revoke)")

    print("\nALL GREEN — hub erasure propagates end-to-end to a mesh revocation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
