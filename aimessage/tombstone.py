"""Mesh-wide erasure (Phase 5): signed tombstones that revoke a federated memory across the mesh.

When a node forgets one of its memories, it gossips a **tombstone** — an Ed25519-signed statement
"revoker R revokes content-address T". Peers verify it and drop any locally-held copy, but ONLY if
that copy actually originated from R (`origin_node == revoker`). So a node can retract its OWN
memories mesh-wide, and cannot censor anyone else's — the authority is bound to authorship.

Tombstones are durable and idempotent (a replay just re-asserts the revocation), so no freshness
window; dedup is by target. This module is pure (crypto + a bounded log) and fully CI-testable; the
gossip wiring lives in GossipNode, and the "which of my memories were forgotten" source is injected
(Centralaizer's forget suite plugs in later — not built here).
"""
from __future__ import annotations

import base64
import json
import time
from collections import OrderedDict

from nacl.signing import SigningKey, VerifyKey

from .identity import node_id

TOMBSTONE_TAG = b"aimessage/tombstone/v1:"
_SIGNED_FIELDS = ("revoker", "target", "ts", "type")


def _canonical(t: dict) -> bytes:
    return json.dumps({k: t[k] for k in _SIGNED_FIELDS}, sort_keys=True,
                      separators=(",", ":")).encode()


def make_tombstone(target_id: str, key: SigningKey) -> dict:
    """Sign a revocation of content-address `target_id` by this node."""
    t = {"type": "tombstone", "target": target_id,
         "revoker": node_id(key), "ts": round(time.time(), 3)}
    t["sig"] = base64.b64encode(key.sign(TOMBSTONE_TAG + _canonical(t)).signature).decode()
    return t


def verify_tombstone(t: dict) -> bool:
    """True iff `t` is a well-formed tombstone signed by its declared `revoker`."""
    try:
        if not isinstance(t, dict) or t.get("type") != "tombstone" \
                or not isinstance(t.get("target"), str):
            return False
        VerifyKey(base64.b64decode(t["revoker"])).verify(
            TOMBSTONE_TAG + _canonical(t), base64.b64decode(t["sig"]))
        return True
    except Exception:
        return False


class TombstoneLog:
    """Bounded record of verified revocations (target -> revoker). `is_revoked(id)` gates re-serving."""

    def __init__(self, max_entries: int = 10000):
        self._max = max_entries
        self._revoked: "OrderedDict[str, str]" = OrderedDict()

    def record(self, target: str, revoker: str) -> None:
        self._revoked[target] = revoker
        self._revoked.move_to_end(target)
        while len(self._revoked) > self._max:
            self._revoked.popitem(last=False)

    def is_revoked(self, target: str) -> bool:
        return target in self._revoked

    def revoker_of(self, target: str):
        return self._revoked.get(target)

    def __len__(self) -> int:
        return len(self._revoked)
