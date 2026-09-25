"""Phase 7 — mesh → hub write-back.

Absorb accepted mesh answers into the local Centralaizer hub so the asking agent's OWN `memory_search`
returns them next time, instead of the answer living only at the node/CLI. This is the mirror of the
forgotten-poller (which runs hub → mesh for erasure); this runs mesh → hub.

ponytail: ask-triggered only — someone asked, so the answer is wanted. An always-on
subscribe-and-absorb firehose is a separate, later opt-in; don't build it until someone needs it.

Trust boundary (why this is safe to enable):
- The answers handed here are already the corroboration-ranked, trusted-origin-weighted set from
  `broadcast_ask` — that's the mesh-side gate.
- Each write still goes through Centralaizer's own trust gate + PII mask on `/api/memories` (a
  low-trust answer lands in quarantine, not the live store) — that's the hub-side gate.
- Content-address dedup is hub-side too: `write_memory` merges on cosine, so re-absorbing the same
  answer merges instead of duplicating → idempotent.
- We refuse to write back anything this node has locally revoked/forgotten (tombstone-aware).
"""
from __future__ import annotations

import json
import urllib.request


def _post(url: str, body: dict, timeout: float = 3.0) -> dict:
    """POST JSON, return the parsed reply. Module function so tests can swap it (no live hub)."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (localhost hub only)
        return json.loads(r.read())


def write_back(hub_url: str, answers, *, is_revoked=None, timeout: float = 3.0) -> list[str]:
    """Persist accepted mesh `answers` to the local hub. Returns the hub ids that were stored.

    `answers`: PortableMemory-like objects (from broadcast_ask) — `.content`, `.type`, `.id`,
    `.origin_node`. `is_revoked(content_address) -> bool`: skip answers this node has revoked.
    Best-effort: a hub hiccup on one answer never breaks the caller or the others.
    """
    base = hub_url.rstrip("/")
    written: list[str] = []
    for m in answers:
        content = getattr(m, "content", None)
        if not content:
            continue
        addr = getattr(m, "id", "")
        if is_revoked and addr and is_revoked(addr):
            continue                                  # never re-absorb a locally-revoked memory
        meta = {"source": "mesh",
                "origin_node": getattr(m, "origin_node", ""),
                "content_address": addr}              # so a later mesh tombstone can match this copy
        # Freshness passthrough (Phase 7): carry the answer's volatility CLASS so the hub keeps it
        # (write_memory does setdefault → our value wins) instead of re-inferring — a fast-changing
        # fact stays marked fast-changing. Staleness itself is recomputed hub-side from volatility+age.
        vol = (getattr(m, "provenance", None) or {}).get("volatility")
        if vol:
            meta["volatility"] = vol
        body = {
            "agent_id": "mesh-writeback",
            "content": content,                        # already PII-masked upstream (federated tier)
            "memory_type": getattr(m, "type", "semantic"),
            "owner": "shared",
            "metadata": meta,                          # auditable; never looks locally authored
        }
        try:
            res = _post(f"{base}/api/memories", body, timeout=timeout)
        except Exception:
            continue                                  # best-effort — skip this one, keep going
        hub_id = res.get("id") if isinstance(res, dict) else None
        if hub_id and res.get("status") in ("stored", "merged"):
            written.append(hub_id)
    return written
