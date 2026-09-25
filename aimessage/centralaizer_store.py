"""Back a node's store with a running Centralaizer hub — real masking + semantic search.

Consumes Centralaizer's existing `GET /api/search` (localhost:3001). That endpoint returns
content that is ALREADY PII/PHI-masked (Centralaizer masks at write time), so nothing unmasked
reaches this layer. We query `owner=shared` only, so personal / user-owned records are never
exposed to the mesh — that filter is the safety gate.

Drop-in for MemoryStore: same `search(query, limit) -> list[PortableMemory]` shape, so
`Node(store=CentralaizerStore(...))` just works.

ponytail: trust_score defaults to 1.0 (these already passed Centralaizer's trust gate to be
stored as shared). Wire a real per-memory trust_score + an explicit federation tag into
/api/search before opening the mesh to untrusted peers.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from nacl.signing import SigningKey

from .memory import Owner, PortableMemory

DEFAULT_BASE_URL = "http://127.0.0.1:3001"


def _fetch(url: str, timeout: float = 3.0) -> list[dict]:
    """GET a JSON array. Separate module function so tests can swap it."""
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (localhost only)
        return json.loads(r.read())


# Federated-from-hub memories carry a fixed sentinel `created` (the hub's /api/search exposes no real
# timestamp), and a minimal, query-INVARIANT provenance, so a given hub memory always hashes to the
# SAME content-address on this node — which is what makes mesh-wide revocation (tombstones) work.
_FEDERATED_CREATED = 1.0


def hashes_to_revoke(forgotten_ids, fed_map: dict) -> list[str]:
    """Given hub memory-ids the hub has forgotten and our {hub_id -> content_hash} federation map,
    return the content-addresses to tombstone (only memories THIS node actually federated)."""
    return [fed_map[i] for i in forgotten_ids if i in fed_map]


class CentralaizerStore:
    def __init__(self, key: SigningKey, base_url: str = DEFAULT_BASE_URL,
                 allow_shared_as_federated: bool = False):
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.fed_map: dict[str, str] = {}   # hub memory id -> content-address we federated (for revoke)
        # GOVERNANCE (M6): Centralaizer's Owner contract says `shared` = "across local agents on this
        # hub, still on-device". Treating those as mesh-`federated` is a data-egress decision the
        # operator must OPT INTO explicitly. Default OFF → this store federates NOTHING until either
        # the operator opts in here, or Centralaizer exposes a real per-memory federation marker.
        self.allow_shared_as_federated = allow_shared_as_federated

    def add(self, *_args, **_kwargs):
        # Writes must go through Centralaizer's trust-gated memory_write, not here.
        raise NotImplementedError("write via Centralaizer's memory_write")

    def search(self, query: str, limit: int = 5) -> list[PortableMemory]:
        if not self.allow_shared_as_federated:
            return []                          # governance gate: nothing federates without explicit opt-in
        qs = urllib.parse.urlencode({"q": query, "n": limit, "owner": "shared"})
        try:
            rows = _fetch(f"{self.base_url}/api/search?{qs}")
        except Exception:
            return []  # Centralaizer down/unreachable → this node simply answers nothing

        out: list[PortableMemory] = []
        for row in rows:
            m = PortableMemory(
                content=row["content"],  # already masked upstream
                type=row.get("memory_type", "semantic"),
                owner=Owner.FEDERATED.value,
                trust_score=float(row.get("trust_score", 1.0)),   # real hub trust (v0.5), not a default
                created=_FEDERATED_CREATED,                       # sentinel → stable content-address
                # query-invariant provenance ONLY (stays part of the content-address): source, agent,
                # and the memory's volatility CLASS — a stable per-memory property (Phase 7 freshness
                # passthrough). NOT stale/age_days — those are time-dependent and would make the
                # content-address drift every query, breaking tombstone/corroboration matching.
                provenance={"source": "centralaizer", "agent": row.get("agent_id"),
                            "volatility": row.get("volatility")},
            ).sign(self.key)
            out.append(m)
            hub_id = row.get("id")
            if hub_id:
                self.fed_map[hub_id] = m.id                      # content-address we federated → revoke later
        return out
