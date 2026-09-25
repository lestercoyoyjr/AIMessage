"""Edge cases — empty/huge/boundary inputs across the pure layers. CI-safe (no libp2p).

Runnable:  python tests/test_edge.py   |   pytest
"""
import http.client
import json
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import artifact as A
from aimessage.control import serve_control
from aimessage.identity import node_id
from aimessage.memory import Owner, PortableMemory
from aimessage.protocol import (
    answer_query, make_query, open_answer, rank_corroborated, seal_to, verify_query,
)
from aimessage.storage import ArtifactCache
from aimessage.store import MemoryStore


def _fed(content, key):
    return PortableMemory(content=content, type="semantic",
                          owner=Owner.FEDERATED.value, trust_score=0.5).sign(key)


def test_unicode_and_long_content_round_trip():
    k = SigningKey.generate()
    for content in ("中文 🧠 café", "x" * 100_000):        # emoji/CJK and a 100 KB blob
        m = _fed(content, k)
        assert m.verify() and m.content == content
        # content-address stable across (de)serialization
        assert PortableMemory.from_json(m.to_json()).id == m.id


def test_empty_store_and_no_match_return_empty():
    assert MemoryStore().search("anything") == []
    b = SigningKey.generate()
    store = MemoryStore([_fed("chronic care management", b)])
    assert store.search("completely unrelated zebra") == []   # no token overlap → nothing


def test_open_answer_on_empty_and_no_hits():
    a, b = SigningKey.generate(), SigningKey.generate()
    assert open_answer(b"", a) == []                          # empty sealed
    sealed = answer_query(make_query("q", a), MemoryStore(), b)   # responder has nothing
    assert open_answer(sealed, a) == []


def test_empty_query_still_signs_and_verifies():
    q = make_query("", SigningKey.generate())
    assert verify_query(q)                                    # empty query is structurally valid


def test_rank_corroborated_empty():
    assert rank_corroborated([]) == []


def test_cache_quota_exact_boundary():
    k = SigningKey.generate()
    art = A.pack({"f": b"x" * 500}, name="a", kind="plugin", version="1.0", key=k)
    size = len(art.blob)
    c = ArtifactCache(quota_bytes=size)                       # holds exactly one
    c.add(art)
    assert len(c) == 1 and c.total_bytes == size
    art2 = A.pack({"f": b"y" * 500}, name="b", kind="plugin", version="1.0", key=k)
    c.add(art2)                                               # exactly-at-boundary → evict first
    assert len(c) == 1 and art.content_hash not in c and art2.content_hash in c


def test_artifact_with_empty_file():
    k = SigningKey.generate()
    art = A.pack({"empty.txt": b""}, name="e", kind="plugin", version="1.0", key=k)
    assert A.verify(art)
    with tempfile.TemporaryDirectory() as d:
        dest = A.install(art, d, approve=lambda m: True)
        assert (dest / "empty.txt").read_bytes() == b""


def test_owner_tiers_only_federated_shareable():
    k = SigningKey.generate()
    assert PortableMemory(content="x", type="semantic", owner=Owner.FEDERATED.value).sign(k).is_shareable
    assert not PortableMemory(content="x", type="semantic", owner=Owner.SHARED.value).sign(k).is_shareable
    assert not PortableMemory(content="x", type="semantic", owner=Owner.PERSONAL.value).sign(k).is_shareable


def test_control_ask_window_is_clamped():
    captured = []
    srv, token = serve_control(node_id="n", ask=lambda t, w: captured.append(w) or [],
                               ask_min_interval=0.0)   # disable throttle so all 3 reach `ask`
    try:
        h, p = srv.server_address
        for req_window, expect in [(0.1, 0.5), (100.0, 30.0), (4.0, 4.0)]:
            conn = http.client.HTTPConnection(h, p, timeout=3)
            conn.request("POST", "/ask", body=json.dumps({"query": "x", "window": req_window}).encode(),
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            conn.getresponse().read()
            conn.close()
        assert captured == [0.5, 30.0, 4.0]                  # clamped to [0.5, 30]
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
