"""libp2p transport — a real two-node ask/answer over libp2p streams.

Self-skips when the optional libp2p dep (or trio) isn't installed, so CI stays light.
Locally (after `pip install -r requirements-libp2p.txt`) it runs for real.

Runnable:  python tests/test_libp2p.py   |   pytest
"""
import sys
from pathlib import Path


try:
    import trio
    from nacl.signing import SigningKey

    from aimessage.libp2p_transport import Libp2pNode
    from aimessage.memory import Owner, PortableMemory
    from aimessage.store import MemoryStore
    _HAVE_LIBP2P = True
except ImportError:  # ONLY a missing optional dep skips — real bugs in the module must surface
    _HAVE_LIBP2P = False


def test_libp2p_ask_answer_roundtrip():
    if not _HAVE_LIBP2P:
        print("  (skipped: libp2p not installed)")
        return

    kb = SigningKey.generate()
    store = MemoryStore([
        PortableMemory(content="patients on a biologic qualify for chronic care management",
                       type="semantic", owner=Owner.FEDERATED.value, trust_score=0.9).sign(kb),
        PortableMemory(content="withheld personal record", type="episodic",
                       owner=Owner.PERSONAL.value).sign(kb),
    ])
    responder = Libp2pNode(key=kb, store=store)
    asker = Libp2pNode()
    out = {}

    async def scenario():
        async with trio.open_nursery() as nursery:
            async def ready(addr):
                out["addr"] = addr
            nursery.start_soon(lambda: responder.serve(on_ready=ready))
            with trio.move_on_after(30):
                while "addr" not in out:
                    await trio.sleep(0.05)
                out["hits"] = await asker.ask(out["addr"], "chronic care management biologic")
            nursery.cancel_scope.cancel()

    trio.run(scenario)
    hits = out.get("hits", [])
    assert len(hits) == 1, f"expected 1 hit, got {len(hits)}"
    assert hits[0].verify()
    assert "withheld" not in hits[0].content        # owner gate held over libp2p
    # PeerID-as-identity: the returned memory is signed by the responder's identity key.
    import base64
    assert hits[0].origin_node == base64.b64encode(bytes(kb.verify_key)).decode()


if __name__ == "__main__":
    test_libp2p_ask_answer_roundtrip()
    print("ok  test_libp2p_ask_answer_roundtrip")
    print("\ndone")
