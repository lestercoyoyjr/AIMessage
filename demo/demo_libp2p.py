"""Phase 3 demo over a REAL libp2p transport (Noise-encrypted, multiaddr-addressed).

Same primitive as demo.py, but the two nodes talk over libp2p streams instead of a raw
socket — the path that works across machines/NAT. Two hosts run in one process here for a
self-contained demo; the responder's multiaddr is exactly what a second machine would dial.

Needs the optional dep:  .venv/bin/pip install -r requirements-libp2p.txt
Run:                      .venv/bin/python demo/demo_libp2p.py
"""
import sys
from pathlib import Path

import trio
from nacl.signing import SigningKey

from aimessage.libp2p_transport import Libp2pNode  # noqa: E402
from aimessage.memory import Owner, PortableMemory  # noqa: E402
from aimessage.store import MemoryStore  # noqa: E402


def short(nid: str) -> str:
    return nid[:12] + "…"


def responder_store(key: SigningKey) -> MemoryStore:
    seed = [
        PortableMemory(content="Patients with ORG_1 insurance on a biologic (3-month "
                               "intervals) qualify for the CMS Chronic Care Management program.",
                       type="semantic", owner=Owner.FEDERATED.value, trust_score=0.9),
        PortableMemory(content="Patient PERSON_2 chronic care enrollment pending.",
                       type="episodic", owner=Owner.PERSONAL.value, trust_score=0.95),
    ]
    return MemoryStore([m.sign(key) for m in seed])


async def main() -> int:
    kb = SigningKey.generate()
    responder = Libp2pNode(key=kb, store=responder_store(kb), name="clinic-B")
    asker = Libp2pNode(name="agent-A")

    result = {}
    async with trio.open_nursery() as nursery:
        async def ready(addr):
            result["addr"] = addr

        nursery.start_soon(lambda: responder.serve(on_ready=ready))
        with trio.move_on_after(30):
            while "addr" not in result:
                await trio.sleep(0.05)
            print(f"Responder libp2p multiaddr:\n  {result['addr']}\n")
            print('Asker dials it and asks: "which patients qualify for chronic care management?"\n')
            hits = await asker.ask(result["addr"], "which patients qualify for chronic care management")

            print(f"→ {len(hits)} verified, decrypted result(s) over libp2p:")
            for m in hits:
                print(f"   • [{m.type}] trust={m.trust_score} from {short(m.origin_node)} "
                      f"verified={m.verify()}")
                print(f"     {m.content}")
            leaked = any("PERSON_2" in m.content for m in hits)
            print(f"\nPERSONAL record withheld at the owner gate: {not leaked}")
            ok = len(hits) == 1 and not leaked
            print("\nLIBP2P DEMO PASSED ✅" if ok else "\nLIBP2P DEMO FAILED ❌")
            result["ok"] = ok
        nursery.cancel_scope.cancel()

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(trio.run(main))
