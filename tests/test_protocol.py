"""Phase 3 checks: query auth, E2E seal, owner gate over the wire, end-to-end ask.

Runnable:  python tests/test_protocol.py   |   pytest
"""
import sys
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.signing import SigningKey

from aimessage.memory import Owner, PortableMemory  # noqa: E402
from aimessage.node import Node  # noqa: E402
from aimessage.protocol import (  # noqa: E402
    answer_query, handle_query, make_query, open_answer, open_answer_envelope, open_sealed,
    seal_to, verify_query,
)
from aimessage.store import MemoryStore  # noqa: E402
import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402
from aimessage.identity import node_id as _node_id  # noqa: E402


def _fed(content, key, trust=0.8):
    return PortableMemory(content=content, type="semantic",
                          owner=Owner.FEDERATED.value, trust_score=trust).sign(key)


def test_query_sign_and_verify():
    q = make_query("hello world", SigningKey.generate())
    assert verify_query(q)


def test_query_tamper_rejected():
    q = make_query("hello world", SigningKey.generate())
    q["query"] = "malicious swap"
    assert not verify_query(q)


def test_seal_open_roundtrip():
    k = SigningKey.generate()
    from aimessage.identity import node_id
    sealed = seal_to(node_id(k), b"secret payload")
    assert open_sealed(k, sealed) == b"secret payload"


def test_wrong_recipient_cannot_open():
    k, eve = SigningKey.generate(), SigningKey.generate()
    from aimessage.identity import node_id
    sealed = seal_to(node_id(k), b"secret payload")
    # Catch ONLY the expected crypto failure and assert OUTSIDE the try — otherwise a
    # non-raising open_sealed (broken confidentiality) would be silently swallowed.
    decrypted = None
    try:
        decrypted = open_sealed(eve, sealed)
    except CryptoError:
        pass  # expected: the eavesdropper's key cannot open the sealed box
    assert decrypted is None, f"eavesdropper decrypted the sealed box: {decrypted!r}"


def test_answer_excludes_non_federated():
    b = SigningKey.generate()
    store = MemoryStore([
        _fed("chronic care management for biologic patients", b),
        PortableMemory(content="personal chronic note", type="episodic",
                       owner=Owner.PERSONAL.value).sign(b),
        PortableMemory(content="shared chronic note", type="semantic",
                       owner=Owner.SHARED.value).sign(b),
    ])
    a = SigningKey.generate()
    sealed = answer_query(make_query("chronic care", a), store, b)
    hits = open_answer(sealed, a)
    assert len(hits) == 1
    assert all(m.owner == Owner.FEDERATED.value for m in hits)


def test_forged_memory_dropped_by_asker():
    # A memory signed by nobody (bad sig) must not survive open_answer.
    b, a = SigningKey.generate(), SigningKey.generate()
    good = _fed("chronic care management guidance", b)
    forged = _fed("chronic care management guidance", b)
    forged.content = "tampered after signing"  # invalidates sig
    store = MemoryStore([good, forged])
    sealed = answer_query(make_query("chronic care", a), store, b)
    hits = open_answer(sealed, a)
    assert all(m.verify() for m in hits)
    assert not any(m.content == "tampered after signing" for m in hits)


def test_end_to_end_ask_over_socket():
    b = SigningKey.generate()
    responder = Node(key=b, store=MemoryStore([
        _fed("patients on a biologic qualify for chronic care management", b, trust=0.9),
        PortableMemory(content="withheld personal record", type="episodic",
                       owner=Owner.PERSONAL.value).sign(b),
    ]))
    addr = responder.serve()
    try:
        asker = Node()
        hits = asker.ask(addr, "chronic care management biologic")
        assert len(hits) == 1
        assert hits[0].verify()
        assert "withheld" not in hits[0].content
    finally:
        responder.stop()


def test_forged_query_gets_no_answer():
    b = SigningKey.generate()
    responder = Node(key=b, store=MemoryStore([_fed("some federated fact", b)]))
    addr = responder.serve()
    try:
        import json
        from aimessage.transport import send_request
        forged = make_query("anything", SigningKey.generate())
        forged["asker_node"] = __import__("base64").b64encode(
            bytes(SigningKey.generate().verify_key)).decode()  # sig no longer matches
        reply = send_request(addr[0], addr[1], json.dumps(forged).encode())
        assert reply == b""  # responder refused to answer
    finally:
        responder.stop()


# --- P0.2 adversarial: a hostile peer must not be able to crash the asker (C1/M8) ---

def _sealed_answer(mem_jsons, asker_key, responder_key=None, query_nonce=None):
    """Build a properly responder-SIGNED answer sealed to the asker (P0.5: answers are authenticated
    and query-nonce-bound). `responder_key`/`query_nonce` default to a fresh key / None."""
    from aimessage.protocol import ANSWER_TAG, _ANSWER_SIGNED_FIELDS, _canonical
    rk = responder_key or SigningKey.generate()
    payload = {"type": "answer", "responder_node": _node_id(rk),
               "query_nonce": query_nonce, "memories": mem_jsons}
    payload["sig"] = _b64.b64encode(
        rk.sign(ANSWER_TAG + _canonical(payload, _ANSWER_SIGNED_FIELDS)).signature).decode()
    return seal_to(_node_id(asker_key), _json.dumps(payload).encode())


def test_open_answer_survives_garbage_ciphertext():
    a = SigningKey.generate()
    # Pre-fix this raised CryptoError out of open_sealed and crashed the asker.
    assert open_answer(b"\x00\x01 not a sealed box", a) == []


def test_open_answer_ignores_non_dict_payload():
    a = SigningKey.generate()
    sealed = seal_to(_node_id(a), _json.dumps([1, 2, 3]).encode())  # a list, not an object
    assert open_answer(sealed, a) == []


def test_open_answer_drops_malformed_memory_blobs_keeps_good():
    a, b = SigningKey.generate(), SigningKey.generate()
    good = _fed("real federated fact", b).to_json()
    sealed = _sealed_answer([good, '{"bogus": 1}', "not even json", "[]"], a)
    hits = open_answer(sealed, a)
    assert len(hits) == 1 and hits[0].verify()


def test_open_answer_rejects_unsigned_answer():
    # An answer with no valid responder signature is refused (responder attribution can't be spoofed).
    a = SigningKey.generate()
    payload = {"type": "answer", "responder_node": "x", "query_nonce": None,
               "memories": [_fed("x", SigningKey.generate()).to_json()], "sig": "AAAA"}
    sealed = seal_to(_node_id(a), _json.dumps(payload).encode())
    assert open_answer(sealed, a) == []


def test_answer_transplant_rejected_by_nonce_binding():
    # An answer built for one query nonce must not satisfy a different query (M4).
    a, b = SigningKey.generate(), SigningKey.generate()
    sealed = _sealed_answer([_fed("fact", b).to_json()], a, responder_key=b, query_nonce="OLD")
    assert open_answer(sealed, a, expected_nonce="OLD")                 # accepted for its own nonce
    assert open_answer(sealed, a, expected_nonce="DIFFERENT") == []     # rejected for another


def test_open_answer_envelope_survives_adversarial_inputs():
    a = SigningKey.generate()
    nonce = "nonce-abc"
    for raw in (
        b"not json at all",
        _json.dumps([1, 2]).encode(),                                  # non-dict envelope
        _json.dumps({"q": "wrong-nonce", "sealed": ""}).encode(),      # wrong nonce
        _json.dumps({"q": nonce}).encode(),                            # missing sealed
        _json.dumps({"q": nonce, "sealed": "!!not base64!!"}).encode(),
        _json.dumps({"q": nonce, "sealed": _b64.b64encode(b"garbage").decode()}).encode(),
    ):
        assert open_answer_envelope(raw, nonce, a) == (None, [])       # must never raise


def test_open_answer_envelope_accepts_valid():
    a, b = SigningKey.generate(), SigningKey.generate()
    nonce = "nonce-1"
    sealed = _sealed_answer([_fed("shared fact", b).to_json()], a, responder_key=b, query_nonce=nonce)
    env = _json.dumps({"q": nonce, "sealed": _b64.b64encode(sealed).decode()}).encode()
    responder, hits = open_answer_envelope(env, nonce, a)
    assert responder == _node_id(b) and len(hits) == 1 and hits[0].verify()


def test_handle_query_answers_once_then_rejects_replay():
    from aimessage.protocol import ReplayGuard
    b = SigningKey.generate()
    store = MemoryStore([_fed("federated fact about cats", b)])
    a = SigningKey.generate()
    raw = _json.dumps(make_query("cats", a)).encode()
    nonce = _json.loads(raw)["nonce"]
    guard = ReplayGuard()
    first = handle_query(raw, store, b, guard)
    second = handle_query(raw, store, b, guard)      # the exact same signed query, replayed
    assert first != b"" and second == b""            # answered once; replay refused
    assert len(open_answer(first, a, expected_nonce=nonce)) == 1   # the first answer is valid + bound


def test_replay_guard_rejects_stale_and_repeated():
    from aimessage.protocol import ReplayGuard
    clock = {"t": 1000.0}
    g = ReplayGuard(window_s=30.0, clock=lambda: clock["t"])
    q = make_query("hello", SigningKey.generate())
    q["ts"] = 1000.0
    assert g.check(q) is True            # fresh + new nonce
    assert g.check(q) is False           # same nonce → replay
    q2 = make_query("hi", SigningKey.generate())
    q2["ts"] = 900.0                     # 100s old, outside the 30s window
    assert g.check(q2) is False          # stale


def test_replay_guard_memory_hard_capped():
    # NEW-1: memory never exceeds max_entries even under a flood of fresh, unique, valid nonces.
    from aimessage.protocol import ReplayGuard
    clock = {"t": 1000.0}
    g = ReplayGuard(window_s=30.0, clock=lambda: clock["t"], max_entries=10)
    for i in range(10):
        assert g.check({"ts": 1000.0, "nonce": f"n{i}"}) is True
    assert len(g._seen) == 10
    assert g.check({"ts": 1000.0, "nonce": "overflow"}) is False   # full → fail closed
    assert len(g._seen) <= 10                                       # bounded
    clock["t"] = 1000.0 + 31                                        # window elapses → prune frees room
    assert g.check({"ts": 1031.0, "nonce": "later"}) is True
    assert len(g._seen) <= 10


def test_replay_guard_no_boundary_replay_after_prune():
    # NEW-2: an entry pruned because the clock advanced is one whose ts is already stale, so a replay
    # of it is rejected by the freshness check — never accepted.
    from aimessage.protocol import ReplayGuard
    clock = {"t": 1000.0}
    g = ReplayGuard(window_s=30.0, clock=lambda: clock["t"], max_entries=1)
    assert g.check({"ts": 1000.0, "nonce": "a"}) is True
    clock["t"] = 1000.0 + 31                                        # "a" (ts=1000) now stale
    assert g.check({"ts": 1000.0, "nonce": "a"}) is False          # replay of stale ts → rejected


def test_ranking_counts_origins_not_relayers():
    # NEW-3: 5 relayers of ONE origin's memory must NOT outrank a 2-distinct-origin corroboration.
    from aimessage.protocol import rank_corroborated
    oA = SigningKey.generate()
    mA = _fed("content A", oA)
    relayed_A = [(f"relayer{i}", mA) for i in range(5)]            # 5 relayers, 1 origin
    grp_B = [("rx", _fed("content B", SigningKey.generate())),
             ("ry", _fed("content B", SigningKey.generate()))]     # 2 distinct origins
    ranked = rank_corroborated(relayed_A + grp_B)
    assert ranked[0].content == "content B"                        # 2 origins > 5 relayers of 1 origin


def test_ranking_prefers_trusted_origin():
    from aimessage.protocol import rank_corroborated
    oT = SigningKey.generate()
    trusted = {_node_id(oT)}
    grp = [("r", _fed("trusted fact", oT))]                        # 1 trusted origin
    untrusted = [("a", _fed("untrusted fact", SigningKey.generate())),
                 ("b", _fed("untrusted fact", SigningKey.generate()))]  # 2 untrusted origins
    ranked = rank_corroborated(grp + untrusted, trusted_origins=trusted)
    assert ranked[0].content == "trusted fact"                     # trust beats raw corroboration count


def test_from_json_rejects_non_dict_and_ignores_extra_keys():
    m = _fed("keep me", SigningKey.generate())
    doc = _json.loads(m.to_json())
    doc["evil_extra"] = "smuggled"
    m2 = PortableMemory.from_json(_json.dumps(doc))
    assert m2.verify() and m2.content == "keep me"     # extra key ignored, still verifies
    raised = False
    try:
        PortableMemory.from_json("[1, 2, 3]")
    except TypeError:
        raised = True
    assert raised, "from_json must reject a non-object"


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
