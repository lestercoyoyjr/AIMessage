"""P2a — bounded LRU artifact cache. Pure stdlib → CI.

Runnable:  python tests/test_storage.py   |   pytest
"""
import sys
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import artifact as A  # noqa: E402
from aimessage.artifact_exchange import ArtifactHost, fetch  # noqa: E402
from aimessage.storage import ArtifactCache  # noqa: E402

_K = SigningKey.generate()


def _art(name, filler=1000):
    # equal-size blobs (same structure) so quota math is exact across a/b/c
    return A.pack({"f.txt": b"x" * filler, "n": name.encode()},
                  name=name, kind="plugin", version="1.0", key=_K)


def test_add_get_and_total_bytes():
    a = _art("a")
    c = ArtifactCache(quota_bytes=10 * len(a.blob))
    c.add(a)
    assert len(c) == 1 and c.total_bytes == len(a.blob)
    assert c.get(a.content_hash) is a and c.get("missing") is None


def test_quota_evicts_least_recently_used():
    a, b, cc = _art("a"), _art("b"), _art("c")
    size = len(a.blob)                       # all equal
    cache = ArtifactCache(quota_bytes=2 * size)
    cache.add(a)
    cache.add(b)
    assert len(cache) == 2
    cache.add(cc)                            # over quota → evict oldest (a)
    assert len(cache) == 2 and cache.total_bytes <= 2 * size
    assert a.content_hash not in cache and cc.content_hash in cache


def test_get_marks_recently_used():
    a, b, cc = _art("a"), _art("b"), _art("c")
    size = len(a.blob)
    cache = ArtifactCache(quota_bytes=2 * size)
    cache.add(a)
    cache.add(b)
    cache.get(a.content_hash)                # refresh a → b is now the oldest
    cache.add(cc)                            # evicts b, not a
    assert a.content_hash in cache and b.content_hash not in cache and cc.content_hash in cache


def test_readd_refreshes_without_double_counting():
    a = _art("a")
    cache = ArtifactCache(quota_bytes=10 * len(a.blob))
    cache.add(a)
    cache.add(a)
    assert len(cache) == 1 and cache.total_bytes == len(a.blob)


def test_oversized_artifact_rejected():
    big = _art("big", filler=5000)
    try:
        ArtifactCache(quota_bytes=len(big.blob) - 1).add(big)
        assert False, "an artifact larger than the quota must be rejected"
    except ValueError:
        pass


def test_host_serves_survivor_and_404s_evicted():
    a, b, cc = _art("a"), _art("b"), _art("c")
    size = len(a.blob)
    host = ArtifactHost(quota_bytes=2 * size)
    ha = host.add(a)
    host.add(b)
    hc = host.add(cc)                        # adding c evicts a (oldest)
    addr = host.serve()
    try:
        assert fetch(addr, ha) is None       # evicted → not served
        got = fetch(addr, hc)
        assert got is not None and A.verify(got)
    finally:
        host.stop()


# --- v0.3.0 encrypted cold tier (P2b) ---

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from aimessage.storage import LocalDirColdStore, TieredArtifactCache  # noqa: E402


def test_evicted_artifact_spills_to_cold_as_ciphertext_and_recovers():
    a, b, cc = _art("a"), _art("b"), _art("c")
    size = len(a.blob)
    with tempfile.TemporaryDirectory() as d:
        cold = LocalDirColdStore(d)
        cache = TieredArtifactCache(_K, cold, quota_bytes=2 * size)
        cache.add(a)
        cache.add(b)
        cache.add(cc)                                  # evicts a → spills to cold
        assert a.content_hash not in cache._items      # not hot anymore
        assert a.content_hash in cold                  # but in the cold tier
        raw = cold.get(a.content_hash)                 # cold holds CIPHERTEXT, not the plaintext blob
        assert a.blob not in raw
        got = cache.get(a.content_hash)                # transparent recovery: pull back + decrypt + verify
        assert got is not None and got.content_hash == a.content_hash and A.verify(got)


def test_tampered_cold_blob_is_rejected():
    a, b = _art("a"), _art("b")
    with tempfile.TemporaryDirectory() as d:
        cold = LocalDirColdStore(d)
        cache = TieredArtifactCache(_K, cold, quota_bytes=len(a.blob))   # holds 1
        cache.add(a)
        cache.add(b)                                   # evicts a → spills to cold
        assert a.content_hash in cold
        p = Path(d) / f"{a.content_hash}.enc"
        p.write_bytes(p.read_bytes() + b"tamper")      # corrupt the ciphertext on disk
        assert cache.get(a.content_hash) is None       # decrypt/integrity fails → not served


def test_cold_blob_undecryptable_by_a_different_node():
    a, b = _art("a"), _art("b")
    with tempfile.TemporaryDirectory() as d:
        n1 = TieredArtifactCache(_K, LocalDirColdStore(d), quota_bytes=len(a.blob))
        n1.add(a)
        n1.add(b)                                      # a spilled to cold by node 1
        other = TieredArtifactCache(SigningKey.generate(), LocalDirColdStore(d), quota_bytes=10 ** 9)
        assert other.get(a.content_hash) is None       # different key → can't decrypt node 1's cold blob


def test_cold_store_rejects_non_hex_key():
    with tempfile.TemporaryDirectory() as d:
        cold = LocalDirColdStore(d)
        for bad in ("../escape", "not-a-hash", "/etc/passwd"):
            try:
                cold.put(bad, b"x")
                assert False, f"must reject key {bad!r}"
            except ValueError:
                pass


def test_host_serves_evicted_artifact_from_cold_over_socket():
    a, b, cc = _art("a"), _art("b"), _art("c")
    size = len(a.blob)
    with tempfile.TemporaryDirectory() as d:
        host = ArtifactHost(quota_bytes=2 * size, cold=LocalDirColdStore(d), key=_K)
        ha = host.add(a)
        host.add(b)
        host.add(cc)                                   # evicts a → cold
        addr = host.serve()
        try:
            got = fetch(addr, ha)                      # served from the cold tier, transparently
            assert got is not None and A.verify(got) and got.content_hash == ha
        finally:
            host.stop()


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
