"""Portable, signed memory — the unit that crosses the mesh.

A PortableMemory is a self-contained, Ed25519-signed record. Its `id` is a content
address (sha256 of the signed payload) giving integrity + dedup for free. Only records
with owner == FEDERATED are ever eligible to leave the node (`is_shareable`).

INVARIANT: `content` MUST already be PII/PHI-masked before a PortableMemory is built.
This module does not mask — masking is Centralaizer's job at write time. Signing raw
PHI here would defeat the whole compliance story.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey


class Owner(str, Enum):
    PERSONAL = "personal"    # never leaves the node
    SHARED = "shared"        # shared across local agents on this hub, still on-device
    FEDERATED = "federated"  # eligible to cross the mesh


# Fields covered by the signature (everything except `sig`). Order-independent:
# canonicalization sorts keys, so this tuple is just the membership set.
_SIGNED_FIELDS = (
    "content",
    "created",
    "origin_node",
    "owner",
    "provenance",
    "trust_score",
    "type",
)

# Domain-separation tag so a memory signature can't be replayed as a query/answer signature
# (which carry their own tags). The `id` content-address deliberately does NOT include the tag.
_MEMORY_TAG = b"aimessage/memory/v1:"


@dataclass
class PortableMemory:
    content: str                 # MUST be masked upstream
    type: str                    # semantic | episodic | procedural | relational
    origin_node: str = ""        # b64 Ed25519 public key of the signer (set by sign())
    owner: str = Owner.PERSONAL.value
    provenance: dict = field(default_factory=dict)
    trust_score: float = 0.0     # origin's asserted score; receivers recompute their own
    created: float = 0.0         # unix seconds; stamped at sign() time if 0
    sig: str = ""                # b64 Ed25519 signature over the canonical payload

    def _canonical(self) -> bytes:
        payload = {k: getattr(self, k) for k in _SIGNED_FIELDS}
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    @property
    def id(self) -> str:
        """Content address: stable across (de)serialization and across nodes."""
        return hashlib.sha256(self._canonical()).hexdigest()

    @property
    def is_shareable(self) -> bool:
        """Only FEDERATED records may cross the mesh."""
        return self.owner == Owner.FEDERATED.value

    def sign(self, key: SigningKey) -> "PortableMemory":
        """Stamp origin + created, then sign. Returns self for chaining."""
        if not self.created:
            self.created = round(time.time(), 3)
        self.origin_node = base64.b64encode(bytes(key.verify_key)).decode()
        self.sig = base64.b64encode(key.sign(_MEMORY_TAG + self._canonical()).signature).decode()
        return self

    def verify(self) -> bool:
        """True iff `sig` is a valid signature over the payload by `origin_node`."""
        if not self.sig or not self.origin_node:
            return False
        try:
            vk = VerifyKey(base64.b64decode(self.origin_node))
            vk.verify(_MEMORY_TAG + self._canonical(), base64.b64decode(self.sig))
            return True
        except (BadSignatureError, ValueError):
            return False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> "PortableMemory":
        # Defensive: the JSON may come from an untrusted peer. Reject non-objects and drop
        # unknown keys (a peer can't smuggle extra fields — they aren't signed anyway). A
        # missing REQUIRED field still raises TypeError, which callers catch and skip.
        data = json.loads(s)
        if not isinstance(data, dict):
            raise TypeError("portable memory must be a JSON object")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
