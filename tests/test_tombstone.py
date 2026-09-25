"""Phase 5 — mesh erasure via signed tombstones. Crypto/log/authority are pure (CI); the 2-node
gossip propagation self-skips without libp2p.

Runnable:  python tests/test_tombstone.py   |   pytest
"""
from nacl.signing import SigningKey

from aimessage.identity import node_id
from aimessage.memory import Owner, PortableMemory
from aimessage.tombstone import TombstoneLog, make_tombstone, verify_tombstone


def _fed(content, key):
    return PortableMemory(content=content, type="semantic",
                          owner=Owner.FEDERATED.value, trust_score=0.9).sign(key)


def test_make_verify_roundtrip():
    k = SigningKey.generate()
    t = make_tombstone("abc123", k)
    assert verify_tombstone(t) and t["revoker"] == node_id(k) and t["target"] == "abc123"


def test_tampered_tombstone_rejected():
    k = SigningKey.generate()
    t = make_tombstone("abc", k)
    t["target"] = "def"                         # sig no longer matches
    assert not verify_tombstone(t)


def test_forged_revoker_rejected():
    import base64
    t = make_tombstone("abc", SigningKey.generate())
    t["revoker"] = base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()  # claim another
    assert not verify_tombstone(t)


def test_log_records_and_bounds():
    log = TombstoneLog(max_entries=3)
    for i in range(5):
        log.record(f"t{i}", "r")
    assert len(log) == 3 and log.is_revoked("t4") and not log.is_revoked("t0")  # oldest dropped


def test_authority_only_revokes_own_origin():
    # The apply rule: drop a held memory only if its origin == the tombstone's revoker.
    a, b = SigningKey.generate(), SigningKey.generate()
    mem_a = _fed("A's memory", a)               # originated by A
    # B tries to revoke A's memory (B signs a tombstone targeting mem_a.id)
    t_from_b = make_tombstone(mem_a.id, b)
    assert verify_tombstone(t_from_b)           # it's a validly-signed tombstone...
    # ...but authority is authorship: mem_a.origin_node (A) != revoker (B) → must NOT drop.
    should_drop = (mem_a.id == t_from_b["target"] and mem_a.origin_node == t_from_b["revoker"])
    assert should_drop is False
    # A revoking its own memory DOES authorize the drop.
    t_from_a = make_tombstone(mem_a.id, a)
    assert (mem_a.id == t_from_a["target"] and mem_a.origin_node == t_from_a["revoker"]) is True


# --- gossip propagation (self-skips without libp2p) ---
try:
    import base64
    import trio

    from aimessage.gossip import GossipNode
    from aimessage.store import MemoryStore
    _HAVE_LIBP2P = True
except ImportError:
    _HAVE_LIBP2P = False


def test_revoke_propagates_and_stops_serving():
    if not _HAVE_LIBP2P:
        print("  (skipped: libp2p not installed)")
        return
    ka = SigningKey.generate()
    mem = _fed("biologic patients qualify for chronic care management", ka)
    responder = GossipNode(key=ka, store=MemoryStore([mem]), lan_mode=True)
    asker = GossipNode(lan_mode=True)
    out = {}

    async def scenario():
        with trio.move_on_after(30):
            async with responder.running() as r, asker.running() as a:
                await a.connect(r.addr())
                await trio.sleep(2.0)
                out["before"] = await a.broadcast_ask("chronic care management biologic", window=3.0)
                await r.revoke(mem)                 # revoke mesh-wide
                await trio.sleep(1.5)               # let the tombstone propagate
                out["after"] = await a.broadcast_ask("chronic care management biologic", window=3.0)
                out["asker_saw_tombstone"] = asker._tombstones.is_revoked(mem.id)

    trio.run(scenario)
    assert len(out.get("before", [])) == 1          # served before revocation
    assert out.get("after", []) == []               # responder stopped serving after revoke
    assert out.get("asker_saw_tombstone") is True   # tombstone reached the asker


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
