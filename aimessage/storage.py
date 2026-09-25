"""Bounded local artifact cache (P2a) — the storage tier the Drive overflow (v0.3.0) plugs into.

Only ARTIFACTS (plugin / MCP-server blobs) live here; they're content-addressed, immutable, and
re-fetchable by hash, which makes them safe to evict. Memories are NOT cached here — they stay fully
local and are never evicted.

The cache holds total blob bytes <= `quota_bytes` (default 10 GiB). Adding over quota evicts
least-recently-used artifacts first; `get` (a serve/fetch hit) marks an entry recently used.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path

from nacl.encoding import RawEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b
from nacl.secret import SecretBox
from nacl.signing import SigningKey

from .artifact import Artifact

DEFAULT_QUOTA = 10 * 1024 ** 3   # 10 GiB — applies ONLY to the evictable artifact cache
_HEX64 = re.compile(r"^[0-9a-f]{64}$")   # a content hash: sha256 hex (also a safe filename)


class ArtifactCache:
    def __init__(self, quota_bytes: int = DEFAULT_QUOTA):
        self.quota_bytes = quota_bytes
        self._items: "OrderedDict[str, Artifact]" = OrderedDict()
        self._bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, content_hash: str) -> bool:
        return content_hash in self._items

    def values(self):
        return list(self._items.values())

    def get(self, content_hash: str) -> Artifact | None:
        art = self._items.get(content_hash)
        if art is not None:
            self._items.move_to_end(content_hash)     # mark recently used (LRU)
        return art

    def add(self, art: Artifact) -> str:
        size = len(art.blob)
        if size > self.quota_bytes:
            raise ValueError(f"artifact {size}B exceeds cache quota {self.quota_bytes}B")
        if art.content_hash in self._items:           # replace/refresh existing (no double count)
            self._bytes -= len(self._items.pop(art.content_hash).blob)
        while self._bytes + size > self.quota_bytes and self._items:
            _, evicted = self._items.popitem(last=False)   # evict least-recently-used
            self._bytes -= len(evicted.blob)
            self._on_evict(evicted)
        self._items[art.content_hash] = art
        self._bytes += size
        return art.content_hash

    def _on_evict(self, art: Artifact) -> None:
        """Hook: called with each artifact evicted from the hot cache. Base = drop it (re-fetchable
        by content-address). TieredArtifactCache overrides this to spill to an encrypted cold tier."""


# --- v0.3.0 cold tier: encrypted overflow beyond the local quota ---------------------------------
#
# When the hot cache evicts an artifact, a TieredArtifactCache spills it to a ColdStore as CIPHERTEXT
# (client-side SecretBox). A cache miss transparently pulls it back, decrypts, verifies the content
# hash, and re-admits it. The cold store only ever sees encrypted bytes — so even a Google Drive
# synced folder (LocalDirColdStore pointed inside it) holds only ciphertext (zero cleartext egress).
# Only ARTIFACTS reach this path; memories are never in the cache and never spill.


def _cache_key(signing_key: SigningKey) -> bytes:
    """Derive a stable 32-byte SecretBox key from the node's Ed25519 seed (domain-separated), so cold
    blobs are decryptable only by this node — no extra key file to manage."""
    return blake2b(bytes(signing_key), digest_size=32, person=b"aimsg-cache-v1\x00\x00",
                   encoder=RawEncoder)


def _art_to_bytes(art: Artifact) -> bytes:
    return json.dumps({"manifest": art.manifest, "sig": art.sig,
                       "blob": base64.b64encode(art.blob).decode()}).encode()


def _art_from_bytes(data: bytes) -> Artifact:
    d = json.loads(data)
    return Artifact(manifest=d["manifest"], sig=d["sig"], blob=base64.b64decode(d["blob"]))


class LocalDirColdStore:
    """A cold store backed by a directory. Point it at a path inside a Google Drive / Dropbox synced
    folder for the zero-API "lazy Drive" overflow — the OS syncs the (already-encrypted) files."""

    def __init__(self, path: str | Path):
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        if not _HEX64.match(key):                 # keys are content hashes; refuse anything else
            raise ValueError(f"cold-store key must be a sha256 hex digest, got {key!r}")
        return self.dir / f"{key}.enc"

    def put(self, key: str, data: bytes) -> None:
        self._p(key).write_bytes(data)

    def get(self, key: str) -> bytes | None:
        p = self._p(key)
        return p.read_bytes() if p.exists() else None

    def __contains__(self, key: str) -> bool:
        return _HEX64.match(key) is not None and self._p(key).exists()


class TieredArtifactCache(ArtifactCache):
    """Hot local cache (<= quota) backed by an encrypted ColdStore for overflow. Eviction spills to
    cold as ciphertext; a miss pulls back, decrypts, integrity-checks, and re-admits — all transparent
    to callers, so `ArtifactHost` works unchanged."""

    def __init__(self, key: SigningKey, cold, quota_bytes: int = DEFAULT_QUOTA,
                 events=None, full_notice_window_s: float = 300.0, clock=None):
        super().__init__(quota_bytes)
        self._box = SecretBox(_cache_key(key))
        self._cold = cold
        self._events = events                       # optional EventBus for storage.full
        self._full_window = full_notice_window_s
        self._clock = clock or __import__("time").monotonic
        self._last_full_notice = None

    def _on_evict(self, art: Artifact) -> None:
        data = bytes(self._box.encrypt(_art_to_bytes(art)))
        # both-full degradation: if the cold tier can't take it, DROP the evicted artifact (it's
        # re-fetchable by content-address) and surface storage.full — keep serving what's hot.
        free = getattr(self._cold, "free_bytes", lambda: None)()
        if free is not None and free < len(data):
            self._notify_full(len(data), free)
            return
        try:
            self._cold.put(art.content_hash, data)
        except Exception:                           # cold store rejected the write (quota, IO, ...)
            self._notify_full(len(data), free)

    def _notify_full(self, needed: int, free) -> None:
        if self._events is None:
            return
        now = self._clock()
        if self._last_full_notice is not None and now - self._last_full_notice < self._full_window:
            return                                   # throttle: at most one notice per window
        self._last_full_notice = now
        self._events.emit("storage.full", needed=needed, free=free,
                          backend=type(self._cold).__name__)

    def get(self, content_hash: str) -> Artifact | None:
        art = super().get(content_hash)
        if art is not None:
            return art
        sealed = self._cold.get(content_hash)     # cold-tier miss recovery
        if sealed is None:
            return None
        try:
            art = _art_from_bytes(self._box.decrypt(sealed))
        except (CryptoError, ValueError, KeyError, TypeError):
            return None                           # tampered / undecryptable / malformed
        if hashlib.sha256(art.blob).hexdigest() != content_hash:
            return None                           # integrity: cold bytes must match the address
        self.add(art)                             # re-admit to the hot cache (may spill others)
        return art
