"""Phase 7 — mesh → hub write-back. Pure: swaps writeback._post so there's no live hub. CI-safe.

Runnable:  python tests/test_writeback.py   |   pytest
"""
from nacl.signing import SigningKey

from aimessage import writeback as W
from aimessage.memory import Owner, PortableMemory


def _fed(content, key):
    return PortableMemory(content=content, type="semantic",
                          owner=Owner.FEDERATED.value, trust_score=0.8).sign(key)


class _Stub:                       # minimal answer-like object for edge cases
    def __init__(self, content, id="addr", type="semantic", origin_node="orig"):
        self.content, self.id, self.type, self.origin_node = content, id, type, origin_node


class _Capture:
    def __init__(self, status="stored"):
        self.calls, self.status = [], status
    def __call__(self, url, body, timeout=3.0):
        self.calls.append((url, body))
        return {"status": self.status, "id": f"hub-{len(self.calls)}"}


def test_write_back_posts_expected_body():
    k = SigningKey.generate()
    m = _fed("chronic care management", k)
    cap, orig = _Capture(), W._post
    W._post = cap
    try:
        ids = W.write_back("http://127.0.0.1:3001", [m])
    finally:
        W._post = orig
    assert ids == ["hub-1"]
    url, body = cap.calls[0]
    assert url.endswith("/api/memories")
    assert body["owner"] == "shared"
    assert body["content"] == "chronic care management"
    assert body["memory_type"] == "semantic"
    assert body["metadata"]["source"] == "mesh"
    assert body["metadata"]["content_address"] == m.id          # so a tombstone can match later
    assert body["metadata"]["origin_node"] == m.origin_node


def test_write_back_skips_locally_revoked():
    k = SigningKey.generate()
    m = _fed("secret that was revoked here", k)
    cap, orig = _Capture(), W._post
    W._post = cap
    try:
        ids = W.write_back("http://h", [m], is_revoked=lambda a: a == m.id)
    finally:
        W._post = orig
    assert ids == [] and cap.calls == []                        # never even POSTed a revoked answer


def test_write_back_is_best_effort_on_hub_error():
    k = SigningKey.generate()
    good, bad = _fed("keep this", k), _fed("boom", k)

    def flaky(url, body, timeout=3.0):
        if body["content"] == "boom":
            raise OSError("hub down")
        return {"status": "stored", "id": "hub-x"}

    orig = W._post
    W._post = flaky
    try:
        ids = W.write_back("http://h", [bad, good])             # bad first → good must still land
    finally:
        W._post = orig
    assert ids == ["hub-x"]


def test_write_back_skips_empty_content():
    cap, orig = _Capture(), W._post
    W._post = cap
    try:
        ids = W.write_back("http://h", [_Stub("")])
    finally:
        W._post = orig
    assert ids == [] and cap.calls == []                        # empty content → no POST


def test_write_back_counts_only_stored_or_merged():
    cap, orig = _Capture(status="quarantined"), W._post         # hub trust-gated it into quarantine
    W._post = cap
    try:
        ids = W.write_back("http://h", [_Stub("low-trust mesh claim")])
    finally:
        W._post = orig
    assert len(cap.calls) == 1 and ids == []                    # POSTed, but not counted as written


def test_write_back_carries_volatility_from_provenance():
    # Phase 7 freshness: a fast-changing answer keeps its volatility class on write-back so the hub
    # doesn't re-infer it as durable.
    s = _Stub("BTC is at $X right now"); s.provenance = {"volatility": "transient"}
    cap, orig = _Capture(), W._post
    W._post = cap
    try:
        W.write_back("http://h", [s])
    finally:
        W._post = orig
    assert cap.calls[0][1]["metadata"]["volatility"] == "transient"


def test_write_back_omits_volatility_when_absent():
    cap, orig = _Capture(), W._post
    W._post = cap
    try:
        W.write_back("http://h", [_Stub("no volatility signal")])   # _Stub has no .provenance
    finally:
        W._post = orig
    assert "volatility" not in cap.calls[0][1]["metadata"]


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
