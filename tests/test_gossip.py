"""Gossipsub broadcast discovery — real 2-node mesh, one broadcasts and the other answers.

Self-skips when libp2p/trio absent (CI stays light). Locally it runs a real mesh, so it's
timing-sensitive; the window is generous and it's bounded so it can't hang.

Runnable:  python tests/test_gossip.py   |   pytest
"""
import sys
from pathlib import Path


try:
    import base64

    import trio
    from nacl.signing import SigningKey

    from aimessage.gossip import GossipNode
    from aimessage.memory import Owner, PortableMemory
    from aimessage.store import MemoryStore
    _HAVE_LIBP2P = True
except ImportError:  # ONLY a missing optional dep skips — real bugs in the module must surface
    _HAVE_LIBP2P = False


def test_gossip_broadcast_discovery():
    if not _HAVE_LIBP2P:
        print("  (skipped: libp2p not installed)")
        return

    k_ask, k_hit = SigningKey.generate(), SigningKey.generate()
    knower_store = MemoryStore([
        PortableMemory(content="patients on a biologic qualify for chronic care management",
                       type="semantic", owner=Owner.FEDERATED.value, trust_score=0.9).sign(k_hit),
        PortableMemory(content="withheld personal record", type="episodic",
                       owner=Owner.PERSONAL.value).sign(k_hit),
    ])
    asker = GossipNode(lan_mode=True, key=k_ask)
    knower = GossipNode(lan_mode=True, key=k_hit, store=knower_store)
    out = {}

    async def scenario():
        with trio.move_on_after(20):  # hard safety bound (inside trio context)
            async with asker.running() as a, knower.running() as kn:
                await a.connect(kn.addr())
                await trio.sleep(2.5)  # graft the mesh
                out["hits"] = await a.broadcast_ask("chronic care management biologic", window=4.0)

    trio.run(scenario)

    hits = out.get("hits", [])
    assert len(hits) == 1, f"expected 1 answer from the mesh, got {len(hits)}"
    assert hits[0].verify()
    assert "withheld" not in hits[0].content  # personal record never broadcast
    assert hits[0].origin_node == base64.b64encode(bytes(k_hit.verify_key)).decode()


if __name__ == "__main__":
    test_gossip_broadcast_discovery()
    print("ok  test_gossip_broadcast_discovery")
    print("\ndone")
