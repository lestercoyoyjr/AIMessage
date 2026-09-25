"""Mesh WIRE hop of a revocation — a real 2-node mesh: one node revokes a content-address and the
other RECEIVES the signed tombstone over gossipsub and drops its copy.

This closes the gap the forget->revoke smoke left open: the smoke proved the HTTP->tombstone chain
and the serve-time drop on ONE node, but not that a tombstone actually propagates node A -> node B
over the wire. (make_tombstone/verify_tombstone/_apply_tombstone are unit-tested; this proves the
pubsub hop + the authorship-bound drop together.)

Self-skips when libp2p/trio absent (CI stays light). Timing-sensitive but hard-bounded so it can't hang.

Runnable:  python tests/test_revoke_wire.py   |   pytest
"""
try:
    import trio
    from nacl.signing import SigningKey

    from aimessage.gossip import GossipNode
    from aimessage.memory import Owner, PortableMemory
    from aimessage.store import MemoryStore
    _HAVE_LIBP2P = True
except ImportError:  # ONLY a missing optional dep skips — real bugs in the module must surface
    _HAVE_LIBP2P = False

_QUERY = "chronic care management biologic"


def test_tombstone_propagates_over_the_wire_and_drops_the_copy():
    if not _HAVE_LIBP2P:
        print("  (skipped: libp2p not installed)")
        return

    k_a, k_b = SigningKey.generate(), SigningKey.generate()
    # A ORIGINATES the memory (signs it) → its tombstone (revoker == A) carries authority to drop it.
    # Both nodes hold the SAME signed copy, as if B had federated it from the mesh earlier.
    m = PortableMemory(content="patients on a biologic qualify for chronic care management",
                       type="semantic", owner=Owner.FEDERATED.value, trust_score=0.9).sign(k_a)
    revoker = GossipNode(lan_mode=True, key=k_a, store=MemoryStore([m]))
    holder = GossipNode(lan_mode=True, key=k_b, store=MemoryStore([m]))
    out = {}

    def served_on_holder():
        return [h for h in holder.store.search(_QUERY) if not holder._tombstones.is_revoked(h.id)]

    async def scenario():
        with trio.move_on_after(25):  # hard safety bound (inside trio context)
            async with revoker.running() as r, holder.running() as h:  # noqa: F841
                await r.connect(h.addr())
                await trio.sleep(2.5)                       # graft the tombstone topic mesh
                out["before_revoked"] = holder._tombstones.is_revoked(m.id)
                out["before_served"] = any(x.id == m.id for x in served_on_holder())

                await r.revoke_hash(m.id)                   # publish the signed tombstone to the mesh
                for _ in range(48):                         # wait (bounded) for it to arrive + apply
                    if holder._tombstones.is_revoked(m.id):
                        break
                    await trio.sleep(0.25)

                out["after_revoked"] = holder._tombstones.is_revoked(m.id)
                out["after_in_store"] = any(x.id == m.id for x in holder.store.memories)
                out["after_served"] = any(x.id == m.id for x in served_on_holder())

    trio.run(scenario)

    assert out.get("before_revoked") is False, "holder shouldn't know the revocation before it's sent"
    assert out.get("before_served") is True, "holder should serve A's copy before revocation"
    assert out.get("after_revoked") is True, "tombstone never reached the holder over the wire"
    assert out.get("after_in_store") is False, "authorship-bound drop didn't remove the copy"
    assert out.get("after_served") is False, "revoked memory is still being served"


if __name__ == "__main__":
    test_tombstone_propagates_over_the_wire_and_drops_the_copy()
    print("ok  test_tombstone_propagates_over_the_wire_and_drops_the_copy")
    print("\ndone")
