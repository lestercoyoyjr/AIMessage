"""Operator-curated trust anchor — a persisted allowlist of trusted origin node-ids.

The corroboration ranking (`rank_corroborated`) is sybil-limited but not sybil-proof: an attacker can
mint origin keys. The real fix is a trust anchor, and this is the pragmatic one — an explicit,
human-curated allowlist. Memories from a trusted origin rank first regardless of how many identities
a sybil mints. No reputation engine, no auto-trust (TOFU can be gamed); the operator decides.

Stored as a small JSON list of node-ids (base64 Ed25519 public keys). Public data, not secret.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

DEFAULT_TRUST_PATH = Path.home() / ".aimessage" / "trusted.json"


def is_valid_node_id(s) -> bool:
    """A node id is the base64 of a 32-byte Ed25519 public key."""
    try:
        return isinstance(s, str) and len(base64.b64decode(s)) == 32
    except Exception:
        return False


class TrustStore:
    def __init__(self, path: str | Path = DEFAULT_TRUST_PATH):
        self.path = Path(path)
        self._set: set[str] = set()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._set = {x for x in data if is_valid_node_id(x)}   # drop any junk on load
            except Exception:
                self._set = set()

    def add(self, node_id: str) -> None:
        if not is_valid_node_id(node_id):
            raise ValueError("not a valid node id (base64 of a 32-byte Ed25519 public key)")
        self._set.add(node_id)
        self._save()

    def remove(self, node_id: str) -> None:
        self._set.discard(node_id)
        self._save()

    def all(self) -> set[str]:
        return set(self._set)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._set

    def __len__(self) -> int:
        return len(self._set)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self._set)))
