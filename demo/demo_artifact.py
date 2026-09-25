"""Phase 4 demo — share an MCP server between nodes, safely.

A publisher packs an (inert) MCP-server bundle into a signed, content-addressed artifact and serves
it. A consumer fetches it by hash, verifies integrity + signature, sees tampering rejected, has the
install REFUSED without approval, then approves and installs (extract only — never run).

Run:  .venv/bin/python demo/demo_artifact.py
"""
import sys
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import artifact as A  # noqa: E402
from aimessage.artifact_exchange import ArtifactHost, fetch  # noqa: E402

BUNDLE = {
    "server.py": b"# demo MCP server (inert)\n"
                 b"def main():\n    print('weather-mcp up')\n",
    "mcp.json": b'{"name": "weather-mcp", "entry": "server.py"}\n',
    "README.md": b"# weather-mcp\nProvides a `get_forecast` tool.\n",
}


def main() -> int:
    publisher_key = SigningKey.generate()
    art = A.pack(BUNDLE, name="weather-mcp", kind="mcp-server", version="0.2.0",
                 key=publisher_key, description="Weather forecast MCP server")
    m = art.manifest
    print("Publisher packed an artifact:")
    print(f"  {m['name']} v{m['version']}  ({m['kind']}, {m['size']} bytes)")
    print(f"  content_hash = {m['content_hash'][:16]}…  publisher = {m['publisher_node'][:12]}…\n")

    host = ArtifactHost()
    host.add(art)
    addr = host.serve()
    print(f"Serving on {addr[0]}:{addr[1]}. Consumer fetches by hash…\n")

    try:
        got = fetch(addr, art.content_hash)
        print(f"Fetched. Integrity + signature verify: {A.verify(got)}")

        tampered = A.Artifact(manifest=got.manifest, sig=got.sig, blob=got.blob + b"evil")
        print(f"Tampered copy (one extra byte) verifies: {A.verify(tampered)}  (must be False)\n")

        with tempfile.TemporaryDirectory() as d:
            # 1) No approval → refused.
            refused = False
            try:
                A.install(got, d, approve=lambda mani: False)
            except PermissionError:
                refused = True
            print(f"Install WITHOUT human approval refused: {refused}")

            # 2) Approve after 'review' → extract only, never run.
            dest = A.install(got, d, approve=lambda mani: True)
            files = sorted(p.name for p in dest.iterdir())
            print(f"Approved → extracted (not executed) to {dest.name}/: {files}")

        ok = A.verify(got) and not A.verify(tampered) and refused
        print("\nARTIFACT DEMO PASSED ✅" if ok else "\nARTIFACT DEMO FAILED ❌")
        return 0 if ok else 1
    finally:
        host.stop()


if __name__ == "__main__":
    raise SystemExit(main())
