"""Phase 6 — join-by-bootstrap discovery (Kademlia DHT rendezvous + gossipsub relay).

The gossip demo manually wired peers (asker was handed the responder's multiaddr). Here the asker
is given ONLY a bootstrap address and still reaches the knower — no direct knower address.

Honest note on mechanism: nodes advertise + look each other up under a DHT rendezvous key, but
py-libp2p 0.7.0's provider lookup is unreliable on a 3-node localhost net (the asker direction
often finds 0). What carries the demo is that everyone connects to the bootstrap and gossipsub
relays multi-hop through it — so "one address to join" holds. The DHT layer is wired for real-scale
networks where find_providers does the work; here it's best-effort on top of the bootstrap relay.

Three nodes: a bootstrap anchor, a knower (has the answer), an asker.

Needs the optional dep:  .venv/bin/pip install -r requirements-libp2p.txt
Run:                     .venv/bin/python demo/demo_dht.py
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
    k_knower = SigningKey.generate()
    boot = GossipNode(lan_mode=True, name="bootstrap")
    knower = GossipNode(lan_mode=True, key=k_knower, name="clinic-with-answer",
                        store=store_with(k_knower,
                        "Patients with ORG_1 insurance on a biologic qualify for the CMS "
                        "Chronic Care Management program.", Owner.FEDERATED.value))
    asker = GossipNode(lan_mode=True, name="agent-A")

    async with boot.running() as b, knower.running() as kn, asker.running() as a:
        boot_addr = b.addr()
        print(f"Bootstrap anchor at:\n  {boot_addr}\n")

        # knower joins first so it's advertised before the asker looks.
        await kn.join(boot_addr)
        # asker is handed ONLY the bootstrap address — never the knower's.
        n = await a.join(boot_addr)
        await a.discover(settle=1.0)   # pick up anyone who advertised late
        await trio.sleep(2.5)          # gossipsub graft over the discovered connections

        print(f"Asker {short(a.node_id)} was given ONLY the bootstrap addr.")
        print(f"  DHT direct-peer discovery: {n} (best-effort; often 0 on a tiny localhost net)")
        print("  Mesh reachability: via bootstrap relay (guaranteed)\n")
        print('Asker broadcasts: "which patients qualify for chronic care management?"\n')
        hits = await a.broadcast_ask("which patients qualify for chronic care management", window=4.0)

        import base64
        knower_id = base64.b64encode(bytes(k_knower.verify_key)).decode()
        print(f"→ {len(hits)} verified answer(s) from the mesh:")
        for m in hits:
            print(f"   • from {short(m.origin_node)} verified={m.verify()}")
            print(f"     {m.content}")
        answered = any(m.origin_node == knower_id for m in hits)
        print(f"\nReached the knower knowing only the bootstrap address: {answered}")
        ok = len(hits) == 1 and answered
        print("\nJOIN-BY-BOOTSTRAP DEMO PASSED ✅" if ok else "\nDEMO FAILED ❌")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(trio.run(main))
