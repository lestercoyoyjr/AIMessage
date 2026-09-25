"""Fetch artifacts by content-hash over the network (BitTorrent-style: hash = address).

Minimal exchange over the stdlib socket transport (same framing as the memory-query path), so it
stays CI-testable. A holder serves artifacts it has by hash; a fetcher requests a hash and gets the
signed envelope back, then verifies before it ever touches disk.

ponytail: point-to-point fetch by known hash. Broadcasting *which* artifacts exist (a gossip
catalog) reuses the Phase 6 pattern and can layer on later; libp2p is the real transport, same as
memory queries. The security core (verify + gated install) lives in artifact.py and is transport-
independent — swapping the socket for libp2p changes nothing here.
"""
from __future__ import annotations

import base64
import json

from .artifact import Artifact
from .storage import DEFAULT_QUOTA, ArtifactCache, TieredArtifactCache
from .transport import run_server, send_request

# Artifacts (plugins / MCP servers) are code bundles, not large blobs. Cap the fetch frame at a size
# generous for code but far from OOM — a malicious host you dial can still make you buffer this much
# before verify().
# ponytail: 64 MiB in-memory buffer. If large artifacts ever matter, stream the frame to a temp file
# with incremental hashing instead of buffering — that removes the ceiling entirely (NEW-4 upgrade path).
MAX_ARTIFACT_FRAME = 64 * 1024 * 1024
_HASH_REQUEST_MAX = 1 << 16  # a fetch request is just a hex hash; cap it tiny


class ArtifactHost:
    """Holds artifacts (in a bounded LRU cache) and serves their bytes by content-hash."""

    def __init__(self, quota_bytes: int = DEFAULT_QUOTA, cold=None, key=None):
        # With a cold store + node key, overflow beyond the quota spills to an encrypted cold tier
        # (v0.3.0) and is transparently pulled back on a hit; otherwise evicted blobs are just dropped.
        if cold is not None and key is not None:
            self.cache = TieredArtifactCache(key, cold, quota_bytes)
        else:
            self.cache = ArtifactCache(quota_bytes)
        self._srv = None

    def add(self, art: Artifact) -> str:
        return self.cache.add(art)

    @property
    def total_bytes(self) -> int:
        return self.cache.total_bytes

    def manifests(self) -> list[dict]:
        """The catalog a holder would advertise (manifest + sig, no bytes)."""
        return [{"manifest": a.manifest, "sig": a.sig} for a in self.cache.values()]

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        self._srv = run_server(host, port, self._handle, max_request=_HASH_REQUEST_MAX)
        return self._srv.server_address

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()            # release the listening socket fd (was leaked)
            self._srv = None

    def _handle(self, raw: bytes) -> bytes:
        art = self.cache.get(raw.decode("utf-8", "replace"))   # serve hit → marks recently used
        if not art:
            return b""                          # unknown/evicted hash → empty
        return json.dumps({
            "manifest": art.manifest,
            "sig": art.sig,
            "blob": base64.b64encode(art.blob).decode(),
        }).encode()


def fetch(addr: tuple[str, int], content_hash: str) -> Artifact | None:
    """Request an artifact by hash. Returns it UNVERIFIED — the caller must verify() before use.
    Returns None on an empty/malformed reply (a hostile host can't crash the fetcher)."""
    raw = send_request(addr[0], addr[1], content_hash.encode(), max_reply=MAX_ARTIFACT_FRAME)
    if not raw:
        return None
    try:
        env = json.loads(raw)
        return Artifact(manifest=env["manifest"], sig=env["sig"],
                        blob=base64.b64decode(env["blob"]))
    except (ValueError, TypeError, KeyError):
        return None
