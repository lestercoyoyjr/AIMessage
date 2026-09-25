"""Phase 6 demo — broadcast a query to a mesh, whoever knows answers replies.

Three nodes in one process: an asker + two responders. Only ONE responder has a matching
federated memory; the other holds only a PERSONAL record (must stay silent). The asker doesn't
address anyone — it shouts to the topic and collects sealed answers.

Needs the optional dep:  .venv/bin/pip install -r requirements-libp2p.txt
Run:                     .venv/bin/python demo/demo_gossip.py
"""
import sys
from pathlib import Path

import trio
from nacl.signing import SigningKey

from aimessage.gossip import GossipNode  # noqa: E402
from aimessage.memory import Owner, PortableMemory  # noqa: E402
from aimessage.store import MemoryStore  # noqa: E402


def short(nid: str) -> str:
    return nid[:12] + "…"


def store_with(key, content, owner):
    return MemoryStore([PortableMemory(content=content, type="semantic",
                                       owner=owner, trust_score=0.9).sign(key)])


async def main() -> int:
    k_ask, k_hit, k_miss = (SigningKey.generate() for _ in range(3))
    asker = GossipNode(lan_mode=True, key=k_ask, name="agent-A")
    knower = GossipNode(lan_mode=True, key=k_hit, name="clinic-with-answer",
                        store=store_with(k_hit,
                        "Patients with ORG_1 insurance on a biologic qualify for the CMS "
                        "Chronic Care Management program.", Owner.FEDERATED.value))
    bystander = GossipNode(lan_mode=True, key=k_miss, name="clinic-no-answer",
                           store=store_with(k_miss,
                           "Patient PERSON_2 enrollment pending.", Owner.PERSONAL.value))

    async with asker.running() as a, knower.running() as kn, bystander.running() as by:
        # Wire a small mesh: asker <-> both responders.
        await a.connect(kn.addr())
        await a.connect(by.addr())
        await trio.sleep(2.5)  # let the gossipsub mesh graft

        print(f"Mesh up. Asker {short(a.node_id)} broadcasts to '{'aimessage/queries/1.0.0'}':")
        print('  "which patients qualify for chronic care management?"\n')
        hits = await a.broadcast_ask("which patients qualify for chronic care management", window=4.0)

        print(f"→ {len(hits)} verified, decrypted answer(s) from the mesh:")
        for m in hits:
            print(f"   • from {short(m.origin_node)} trust={m.trust_score} verified={m.verify()}")
            print(f"     {m.content}")

        import base64
        leaked = any("PERSON_2" in m.content for m in hits)
        knower_id = base64.b64encode(bytes(k_hit.verify_key)).decode()
        answered_by_knower = any(m.origin_node == knower_id for m in hits)
        print(f"\nAnswer came from the node that had it: {answered_by_knower}")
        print(f"Bystander's PERSONAL record stayed off the mesh: {not leaked}")
        ok = len(hits) == 1 and answered_by_knower and not leaked
        print("\nGOSSIP DEMO PASSED ✅" if ok else "\nGOSSIP DEMO FAILED ❌")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(trio.run(main))
