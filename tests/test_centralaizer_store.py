"""CentralaizerStore adapter — mocked so CI needs no running Centralaizer.

Runnable:  python tests/test_centralaizer_store.py   |   pytest
"""
import sys
from pathlib import Path

from nacl.signing import SigningKey

import aimessage.centralaizer_store as cs  # noqa: E402
from aimessage.memory import Owner  # noqa: E402


def _swap_fetch(fake):
    """Temporarily replace the module-level _fetch; returns a restore() callable."""
    orig = cs._fetch
    cs._fetch = fake
    return lambda: setattr(cs, "_fetch", orig)


def _store(**kw):
    # Tests of the mapping behavior opt into federation explicitly.
    kw.setdefault("allow_shared_as_federated", True)
    return cs.CentralaizerStore(SigningKey.generate(), **kw)


def test_governance_default_federates_nothing():
    # M6: with the default (opt-in OFF) the store must federate NOTHING — and must not even query
    # Centralaizer — regardless of what the hub would return.
    called = {"n": 0}
    def spy(url, timeout=3.0):
        called["n"] += 1
        return [{"content": "would-be-shared", "memory_type": "semantic"}]
    restore = _swap_fetch(spy)
    try:
        assert cs.CentralaizerStore(SigningKey.generate()).search("anything") == []
        assert called["n"] == 0                 # never reached the network
    finally:
        restore()


def test_maps_masked_rows_to_signed_federated_memories():
    payload = [{
        "content": "EMAIL_1 asked about CCM eligibility for biologic patients",
        "memory_type": "semantic", "agent_id": "clinical-workflow",
        "score": 0.83, "matched_via": "vector",
    }]
    restore = _swap_fetch(lambda url, timeout=3.0: payload)
    try:
        hits = _store().search("ccm", limit=3)
        assert len(hits) == 1
        m = hits[0]
        assert m.verify()                      # signed by the node key
        assert m.is_shareable and m.owner == Owner.FEDERATED.value
        assert m.content == payload[0]["content"]   # masked content passed through
        assert m.provenance["agent"] == "clinical-workflow"
    finally:
        restore()


def test_unreachable_centralaizer_returns_empty():
    def boom(url, timeout=3.0):
        raise OSError("connection refused")
    restore = _swap_fetch(boom)
    try:
        assert _store().search("anything") == []
    finally:
        restore()


def test_query_filters_owner_shared():
    seen = {}
    def capture(url, timeout=3.0):
        seen["url"] = url
        return []
    restore = _swap_fetch(capture)
    try:
        _store(base_url="http://h:9").search("hello world", limit=2)
        assert "owner=shared" in seen["url"]   # personal records never requested
        assert "q=hello+world" in seen["url"]
        assert "n=2" in seen["url"]
    finally:
        restore()


# --- v0.5.0: real trust, stable content-address, federation map, forgotten sweep ---

def test_uses_real_trust_and_stable_content_address():
    row = {"id": "hub-1", "content": "biologic patients qualify for CCM",
           "memory_type": "semantic", "agent_id": "clinical", "trust_score": 0.42,
           "matched_via": "vector", "score": 0.9}
    # Two searches return the SAME row but with query-varying score/matched_via.
    restore = _swap_fetch(lambda url, timeout=3.0: [dict(row, score=0.1, matched_via="fts5")])
    try:
        s = _store()
        h1 = s.search("q1")[0]
        h2 = s.search("q2")[0]                       # different query → different score/matched_via
        assert h1.trust_score == 0.42               # real hub trust, not the 1.0 default
        assert h1.id == h2.id                        # STABLE content-address across queries
        assert s.fed_map["hub-1"] == h1.id          # federation map records hub-id -> content-address
    finally:
        restore()


def test_hashes_to_revoke_maps_forgotten_to_federated():
    from aimessage.centralaizer_store import hashes_to_revoke
    fed_map = {"hub-1": "hashAAA", "hub-2": "hashBBB"}
    # hub forgot hub-1 (we federated it) and hub-9 (we never did)
    assert hashes_to_revoke(["hub-1", "hub-9"], fed_map) == ["hashAAA"]
    assert hashes_to_revoke([], fed_map) == []


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
