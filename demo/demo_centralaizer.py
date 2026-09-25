"""Phase 3 demo, backed by a REAL Centralaizer hub instead of the toy store.

Prereqs (this one needs your stack up, unlike demo.py):
  1. Centralaizer running:  cd ../Centralaizer && .venv/bin/python main.py   (UI on :3001)
  2. Some owner='shared' memories in it that match the query below.

Run:  .venv/bin/python demo/demo_centralaizer.py
"""
import sys
from pathlib import Path

from nacl.signing import SigningKey

from aimessage.node import Node  # noqa: E402

QUERY = "chronic care management biologic patients"


def short(nid: str) -> str:
    return nid[:10] + "…"


def main() -> int:
    responder = Node.with_centralaizer(name="clinic-hub")  # persistent identity + Centralaizer store
    addr = responder.serve()
    print(f"Responder (Centralaizer-backed) on {addr[0]}:{addr[1]}  id={short(responder.node_id)}")

    asker = Node(name="agent-A")  # a different, ephemeral node
    print(f"Asker  id={short(asker.node_id)}\n")
    print(f'A asks: "{QUERY}"\n')

    hits = asker.ask(addr, QUERY)
    print(f"→ {len(hits)} verified, decrypted result(s) from real Centralaizer memory:")
    for m in hits:
        print(f"   • [{m.type}] via={m.provenance.get('matched_via')} "
              f"score={m.provenance.get('centralaizer_score')} verified={m.verify()}")
        print(f"     {m.content}")

    responder.stop()
    if not hits:
        print("\n(no hits) — is Centralaizer running on :3001 with matching owner='shared' memories?")
        print("Everything else is proven by demo.py + the mocked adapter test; this needs the live stack.")
        return 1
    print("\nLIVE DEMO PASSED ✅  (real masked memory, signed + E2E-sealed over the wire)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
