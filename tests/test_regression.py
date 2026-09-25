"""Regression guards — each test pins a specific defect found & fixed during development, so it can
never silently return. CI-safe (pure). See the aimessage-security-review memory for the full list.

Runnable:  python tests/test_regression.py   |   pytest
"""
import http.client
import json
import struct
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import artifact as A
from aimessage import transport as T
from aimessage.centralaizer_store import CentralaizerStore
import aimessage.centralaizer_store as cs
from aimessage.control import serve_control
from aimessage.identity import node_id
from aimessage.memory import Owner, PortableMemory
from aimessage.protocol import ReplayGuard, answer_query, make_query, open_answer
from aimessage.store import MemoryStore


def _fed(content, key):
    return PortableMemory(content=content, type="semantic",
                          owner=Owner.FEDERATED.value, trust_score=0.8).sign(key)


def test_reg_open_answer_never_crashes_on_garbage():
    # C1/M8: open_answer used to raise (CryptoError/TypeError) on hostile bytes → crashed the asker.
    a = SigningKey.generate()
    assert open_answer(b"\x00\x01 not sealed", a) == []


def test_reg_replay_guard_first_request_passes():
    # Bug: last_ask/seen init let the FIRST valid query be wrongly treated as a replay/stale.
    g = ReplayGuard(window_s=30.0, clock=lambda: 1000.0)
    q = make_query("hi", SigningKey.generate()); q["ts"] = 1000.0
    assert g.check(q) is True and g.check(q) is False        # first passes, exact replay rejected


def test_reg_control_first_ask_not_rate_limited():
    # Bug: throttle initialized to 0.0 with a clock at 0.0 → the FIRST ask got 429.
    srv, token = serve_control(node_id="n", ask=lambda t, w: [], clock=lambda: 0.0)
    try:
        h, p = srv.server_address
        conn = http.client.HTTPConnection(h, p, timeout=3)
        conn.request("POST", "/ask", body=b'{"query":"x"}',
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        assert conn.getresponse().status == 200             # not 429
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_reg_recvn_treats_socket_error_as_eof():
    # Bug: a peer resetting mid-read raised ConnectionResetError out of _recvn (found by the
    # oversized-frame integration test). Any OSError must degrade to a short read.
    class ResetSock:
        def recv(self, n):
            raise ConnectionResetError("peer reset")
        def settimeout(self, _):
            pass
    assert T._recvn(ResetSock(), 100) == b""


def test_reg_frame_cap_refuses_without_reading_body():
    # M1: an oversized declared length must be refused BEFORE allocating/reading the body.
    class FakeSock:
        def __init__(self, data): self.buf = bytearray(data)
        def recv(self, n):
            c = bytes(self.buf[:n]); del self.buf[:n]; return c
        def settimeout(self, _): pass
    fs = FakeSock(struct.pack("!I", 2 << 30) + b"x" * 8)
    assert T._read_frame(fs, max_bytes=1024) == b"" and len(fs.buf) == 8   # body untouched


def test_reg_install_rejects_signed_traversal_name():
    # C2: a validly-SIGNED manifest with a traversal name must not escape the dest dir.
    import base64
    import hashlib
    key = SigningKey.generate()
    blob = A._pack_bytes({"x.txt": b"hi"})
    manifest = {"name": "../../../pwned", "kind": "plugin", "version": "1.0",
                "content_hash": hashlib.sha256(blob).hexdigest(), "size": len(blob),
                "publisher_node": node_id(key), "description": ""}
    sig = base64.b64encode(key.sign(A._canonical(manifest)).signature).decode()
    art = A.Artifact(manifest=manifest, sig=sig, blob=blob)
    assert A.verify(art)                                     # signed → verifies...
    with tempfile.TemporaryDirectory() as d:
        parent = Path(d)
        try:
            A.install(art, parent / "box", approve=lambda m: True)
        except ValueError:
            pass                                            # ...but install refuses
        assert not (parent / "pwned").exists()


def test_reg_answer_transplant_rejected():
    # M4: an answer bound to one query nonce must not satisfy another.
    a, b = SigningKey.generate(), SigningKey.generate()
    q = make_query("chronic care", a)
    q["nonce"] = "N1"                                        # answer_query binds this into the seal
    hits = [_fed("chronic care management", b)]
    sealed = answer_query(q, MemoryStore(hits), b, hits=hits)
    assert open_answer(sealed, a, expected_nonce="N1")       # accepted for its own nonce
    assert open_answer(sealed, a, expected_nonce="OTHER") == []   # rejected for a different one


def test_reg_centralaizer_store_stable_content_address():
    # Bug: re-signing each query gave a NEW content-address every time → tombstones couldn't match.
    row = {"id": "hub-1", "content": "biologic patients qualify for CCM", "memory_type": "semantic",
           "agent_id": "clin", "trust_score": 0.7}
    orig = cs._fetch
    cs._fetch = lambda url, timeout=3.0: [dict(row, score=0.1, matched_via="fts5")]
    try:
        s = CentralaizerStore(SigningKey.generate(), allow_shared_as_federated=True)
        assert s.search("q1")[0].id == s.search("q2")[0].id
    finally:
        cs._fetch = orig


def test_reg_from_json_ignores_extra_keys_and_rejects_non_dict():
    # Hardening: a peer can't crash from_json with extra keys or a non-object.
    m = _fed("keep", SigningKey.generate())
    doc = json.loads(m.to_json()); doc["evil"] = "x"
    assert PortableMemory.from_json(json.dumps(doc)).verify()
    try:
        PortableMemory.from_json("[1,2,3]"); assert False
    except TypeError:
        pass


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
