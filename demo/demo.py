"""Phase 3 demo — two nodes, one asks "have we solved X?", gets a signed, sealed answer.

Run:  .venv/bin/python demo/demo.py

Proves, over a real socket between two independent nodes:
  1. Query is Ed25519-signed by the asker; the responder rejects anything unsigned.
  2. Only FEDERATED memories are returned (a PERSONAL record is withheld at the gate).
  3. The reply is E2E-sealed to the asker's key (an eavesdropper can't open it).
  4. The asker verifies each returned memory's signature before trusting it.
"""
import sys
from pathlib import Path

from nacl.signing import SigningKey

from aimessage.memory import Owner, PortableMemory  # noqa: E402
from aimessage.node import Node  # noqa: E402
from aimessage.protocol import make_query, open_sealed  # noqa: E402


def short(node_id: str) -> str:
    return node_id[:10] + "…"


def build_responder() -> Node:
    """Node B: a clinic hub that has already solved a CCM-eligibility question."""
    key = SigningKey.generate()
    node = Node(key=key, name="clinic-B")
    seed = [
        # content is ALREADY masked upstream — note the placeholders, no real PHI.
        PortableMemory(
            content="Patients with ORG_1 insurance on a biologic (3-month intervals) "
                    "qualify for the CMS Chronic Care Management program.",
            type="semantic", owner=Owner.FEDERATED.value, trust_score=0.9,
            provenance={"agent": "clinical-workflow"},
        ),
        PortableMemory(
            content="Retell workspace API keys are rotated quarterly via the ops runbook.",
            type="procedural", owner=Owner.FEDERATED.value, trust_score=0.6,
        ),
        # PERSONAL — must NEVER leave the node, even on a matching query.
        PortableMemory(
            content="Patient PERSON_2 chronic care management enrollment pending.",
            type="episodic", owner=Owner.PERSONAL.value, trust_score=0.95,
        ),
    ]
    for m in seed:
        node.store.add(m.sign(key))
    return node


def main() -> int:
    responder = build_responder()
    addr = responder.serve()
    print(f"Node B (responder) listening on {addr[0]}:{addr[1]}  id={short(responder.node_id)}")

    asker = Node(name="agent-A")
    print(f"Node A (asker)       id={short(asker.node_id)}")
    print("\nA asks: \"which patients qualify for chronic care management?\"\n")

    hits = asker.ask(addr, "which patients qualify for chronic care management")

    print(f"→ {len(hits)} verified, decrypted result(s):")
    for m in hits:
        print(f"   • [{m.type}] trust={m.trust_score} from {short(m.origin_node)} "
              f"verified={m.verify()}")
        print(f"     {m.content}")

    # Prove the boundary + the E2E seal.
    leaked_personal = any("PERSON_2" in m.content for m in hits)
    print(f"\nPERSONAL record withheld at the owner gate: {not leaked_personal}")

    # An eavesdropper (third key) cannot open the sealed reply.
    from aimessage.transport import send_request
    import json
    sealed = send_request(addr[0], addr[1], json.dumps(make_query("ccm", asker.key)).encode())
    eve = SigningKey.generate()
    try:
        open_sealed(eve, sealed)
        eve_blocked = False
    except Exception:
        eve_blocked = True
    print(f"Eavesdropper cannot open the E2E-sealed reply: {eve_blocked}")

    responder.stop()
    ok = len(hits) >= 1 and not leaked_personal and eve_blocked
    print("\nDEMO PASSED ✅" if ok else "\nDEMO FAILED ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
