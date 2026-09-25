"""DHT/bootstrap discovery — a node reaches the mesh knowing ONLY a bootstrap address.

Asserts the property that genuinely works on py-libp2p 0.7.0: join via one bootstrap address,
then broadcast_ask reaches a knower whose address was never shared (via DHT rendezvous where it
works + gossipsub relay through the bootstrap). Does NOT assert direct DHT peer count, which is
unreliable on tiny localhost nets.

Self-skips without libp2p/trio. Runnable:  python tests/test_dht.py  |  pytest
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


def test_reach_mesh_with_only_bootstrap_address():
    if not _HAVE_LIBP2P:
        print("  (skipped: libp2p not installed)")
        return

    k_knower = SigningKey.generate()
    knower_store = MemoryStore([
        PortableMemory(content="patients on a biologic qualify for chronic care management",
                       type="semantic", owner=Owner.FEDERATED.value, trust_score=0.9).sign(k_knower),
        PortableMemory(content="withheld personal record", type="episodic",
                       owner=Owner.PERSONAL.value).sign(k_knower),
    ])
    boot = GossipNode(lan_mode=True, )
    knower = GossipNode(lan_mode=True, key=k_knower, store=knower_store)
    asker = GossipNode(lan_mode=True, )
    out = {}

    async def scenario():
        with trio.move_on_after(30):
            async with boot.running() as b, knower.running() as kn, asker.running() as a:
                await b.advertise()
                await kn.join(b.addr())              # knower joins first
                await a.join(b.addr())               # asker knows ONLY the bootstrap address
                await trio.sleep(2.5)                # gossipsub graft via the bootstrap
                out["hits"] = await a.broadcast_ask("chronic care management biologic", window=4.0)

    trio.run(scenario)
    hits = out.get("hits", [])
    assert len(hits) == 1, f"expected 1 answer reached via bootstrap, got {len(hits)}"
    assert hits[0].verify()
    assert "withheld" not in hits[0].content
    assert hits[0].origin_node == base64.b64encode(bytes(k_knower.verify_key)).decode()


if __name__ == "__main__":
    test_reach_mesh_with_only_bootstrap_address()
    print("ok  test_reach_mesh_with_only_bootstrap_address")
    print("\ndone")
