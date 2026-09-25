"""AIMessage node daemon + CLI.

  python -m aimessage serve  [--listen MADDR] [--bootstrap MADDR]
                             [--centralaizer URL | --memories FILE] [--key PATH]
  python -m aimessage ask    QUERY --bootstrap MADDR [--window S] [--settle S]
  python -m aimessage id     [--key PATH]

`serve` runs a long-lived peer: it answers queries from its federated store and can be a bootstrap
(no --bootstrap) or join one (--bootstrap). `ask` is a one-shot client. Both need the libp2p extra
(`pip install -r requirements-libp2p.txt`); `id` does not.

The store for `serve` is: a running Centralaizer hub (--centralaizer), a JSON file of memories to
serve (--memories), or empty (a relay/asker with nothing of its own to share).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .events import sanitize_text as _sanitize   # single shared sanitizer (peer text → safe one-liner)

from .identity import DEFAULT_KEY_PATH, load_or_create, node_id
from .memory import Owner, PortableMemory
from .store import MemoryStore


def _store_from_file(path: str, key) -> MemoryStore:
    """Load a JSON list of {content, type?, owner?, trust_score?} and sign each with this node's key.
    owner defaults to `federated` (a --memories file is an explicit 'serve these to the mesh' list)."""
    rows = json.loads(Path(path).read_text())
    mems = [
        PortableMemory(
            content=r["content"],
            type=r.get("type", "semantic"),
            owner=r.get("owner", Owner.FEDERATED.value),
            trust_score=float(r.get("trust_score", 0.8)),
        ).sign(key)
        for r in rows
    ]
    return MemoryStore(mems)


def _build_store(args, key):
    if getattr(args, "centralaizer", None):
        from .centralaizer_store import CentralaizerStore
        return CentralaizerStore(key, args.centralaizer,
                                 allow_shared_as_federated=getattr(args, "federate_shared", False))
    if getattr(args, "memories", None):
        return _store_from_file(args.memories, key)
    return MemoryStore()


def _gossip_node():
    try:
        from .gossip import GossipNode
        return GossipNode
    except Exception as e:  # noqa: BLE001 — optional dep
        sys.exit("serve/ask need the libp2p extra:  pip install -r requirements-libp2p.txt\n"
                 f"(import error: {e})")


def _fmt_event(ev) -> str:
    d = ev.detail
    parts = [ev.type]
    if d.get("asker"):
        parts.append(f"from={str(d['asker'])[:12]}…")
    if d.get("peer"):
        parts.append(f"peer={_sanitize(str(d['peer']))}")
    if "query" in d:
        parts.append(f'q="{_sanitize(str(d["query"]))[:60]}"')   # peer content → sanitized
    if "count" in d:
        parts.append(f"hits={d['count']}")
    return "[event] " + " ".join(parts)


def cmd_serve(args) -> None:
    import trio
    from .events import EventBus
    GossipNode = _gossip_node()
    key = load_or_create(args.key)
    store = _build_store(args, key)
    bus = EventBus()
    bus.subscribe(lambda ev: print(_fmt_event(ev), flush=True))   # live event log (always on)
    if args.notify:
        from .notifier import Notifier, os_notify
        Notifier(os_notify, show_preview=args.notify_preview).attach(bus)   # OS desktop notifications
    from .trust import DEFAULT_TRUST_PATH, TrustStore
    trusted = TrustStore(args.trust_file or DEFAULT_TRUST_PATH).all()   # curated anti-sybil allowlist
    node = GossipNode(key=key, store=store, name=args.name, lan_mode=args.lan, events=bus,
                      trusted_origins=trusted)
    held = len(store.memories) if isinstance(store, MemoryStore) else "Centralaizer-backed"

    def _start_control(n):
        """Start the control API once the trio loop is live, bridging /ask → node.broadcast_ask."""
        import functools

        from .control import serve_control
        tok = trio.lowlevel.current_trio_token()

        def ask_bridge(text, window):   # called from the HTTP thread → run on the node's trio loop
            return trio.from_thread.run(functools.partial(n.broadcast_ask, text, window), trio_token=tok)

        mems = (lambda: [m for m in store.memories if m.is_shareable]) \
            if isinstance(store, MemoryStore) else None
        # Phase 7: if this node is Centralaizer-backed and --writeback is on, dashboard "Ask the mesh"
        # absorbs answers into that same hub so the operator's memory_search surfaces them next time.
        wb = None
        if getattr(args, "writeback", False) and getattr(store, "base_url", None):
            from .writeback import write_back
            wb = lambda answers: len(write_back(store.base_url, answers,
                                                is_revoked=n._tombstones.is_revoked))
        ctl, ctl_token = serve_control(node_id=n.node_id, events=bus, ask=ask_bridge,
                                       memories=mems, writeback=wb, port=args.control_port)
        caddr = ctl.server_address
        print(f"CONTROL_ADDR={caddr[0]}:{caddr[1]}", flush=True)
        print(f"CONTROL_TOKEN={ctl_token}", flush=True)
        print(f"DASHBOARD=http://{caddr[0]}:{caddr[1]}/#{ctl_token}", flush=True)   # open in a browser

    async def run():
        async with node.running(listen=args.listen) as n:
            if args.bootstrap:
                connected = await n.join(args.bootstrap)
                print(f"joined bootstrap, connected {connected} peer(s)", flush=True)
            else:
                await n.advertise()
            print(f"NODE_MULTIADDR={n.addr()}", flush=True)
            print(f"node_id={n.node_id}", flush=True)
            if args.control:
                _start_control(n)
            print(f"serving {held} federated memories; Ctrl-C to stop", flush=True)
            async with trio.open_nursery() as nursery:
                from .centralaizer_store import CentralaizerStore
                if isinstance(store, CentralaizerStore):
                    nursery.start_soon(_forgotten_poller, n, store, args.forget_poll_interval)
                    print(f"forgotten-poller on ({args.forget_poll_interval}s): hub erasures → mesh revocations",
                          flush=True)
                await trio.sleep_forever()

    try:
        trio.run(run)
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)


async def _forgotten_poller(node, store, interval: float) -> None:
    """Poll the hub's /api/forgotten and tombstone any federated copy the hub has erased (v0.5.0)."""
    import trio

    from .centralaizer_store import _fetch, hashes_to_revoke
    while True:
        await trio.sleep(interval)
        try:
            rows = await trio.to_thread.run_sync(
                lambda: _fetch(f"{store.base_url}/api/forgotten"))
        except Exception:
            continue
        ids = [r.get("id") for r in rows if isinstance(r, dict)]
        for h in hashes_to_revoke(ids, store.fed_map):
            await node.revoke_hash(h)
            for k in [k for k, v in store.fed_map.items() if v == h]:
                store.fed_map.pop(k, None)          # don't re-revoke the same hash every cycle


def cmd_ask(args) -> None:
    import trio
    from nacl.signing import SigningKey
    GossipNode = _gossip_node()
    # Ask with an ephemeral identity by default so it doesn't collide with a local serving node.
    key = load_or_create(args.key) if args.key else SigningKey.generate()
    node = GossipNode(key=key, name="ask-cli", lan_mode=args.lan)
    out: dict = {}

    async def run():
        async with node.running() as n:
            await n.join(args.bootstrap)
            await trio.sleep(args.settle)   # let the gossipsub mesh graft
            out["hits"] = await n.broadcast_ask(args.query, window=args.window)

    trio.run(run)
    hits = out.get("hits", [])
    print(f"{len(hits)} answer(s):")
    for m in hits:
        # Strip control chars from peer-controlled fields — a hostile responder could embed ANSI
        # escapes to spoof the terminal.
        print(f"- [{_sanitize(m.type)}] from {m.origin_node[:12]}… verified={m.verify()}")
        print(f"    {_sanitize(m.content)}")

    if args.writeback and hits:
        from .writeback import write_back
        ids = write_back(args.writeback, hits, is_revoked=node._tombstones.is_revoked)
        print(f"↳ wrote {len(ids)}/{len(hits)} answer(s) back into {_sanitize(args.writeback)} "
              f"(mesh→hub; your memory_search will surface them)")


def cmd_id(args) -> None:
    print(node_id(load_or_create(args.key)))


def cmd_trust(args) -> None:
    from .trust import DEFAULT_TRUST_PATH, TrustStore
    if args.trust_cmd in ("add", "remove") and not args.node_id:
        sys.exit(f"trust {args.trust_cmd} requires a node id")
    store = TrustStore(args.trust_file or DEFAULT_TRUST_PATH)
    if args.trust_cmd == "add":
        try:
            store.add(args.node_id)
        except ValueError as e:
            sys.exit(str(e))
        print(f"trusted {args.node_id[:16]}… ({len(store)} total)")
    elif args.trust_cmd == "remove":
        store.remove(args.node_id)
        print(f"removed ({len(store)} total)")
    else:  # list
        ids = sorted(store.all())
        print(f"{len(ids)} trusted origin(s):")
        for nid in ids:
            print(f"  {nid}")


def _read_dir(path: str) -> dict:
    """Read a directory into {relative_posix_path: bytes} for packing into an artifact."""
    root = Path(path)
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files[p.relative_to(root).as_posix()] = p.read_bytes()
    if not files:
        sys.exit(f"no files found under {path}")
    return files


def _cold_store(args):
    if getattr(args, "cold_dir", None):
        from .storage import LocalDirColdStore
        return LocalDirColdStore(args.cold_dir)
    return None


def cmd_artifact_serve(args) -> None:
    from . import artifact as A
    from .artifact_exchange import ArtifactHost
    key = load_or_create(args.key)
    art = A.pack(_read_dir(args.path), name=args.name, kind=args.kind, version=args.version,
                 key=key, description=args.description or "")
    host = ArtifactHost(cold=_cold_store(args), key=key)   # cold=None → plain LRU cache
    host.add(art)
    addr = host.serve(port=args.port)
    print(f"serving artifact {art.manifest['name']} v{art.manifest['version']}")
    print(f"CONTENT_HASH={art.content_hash}")
    print(f"ADDR={addr[0]}:{addr[1]}")
    if args.cold_dir:
        print(f"cold overflow → {args.cold_dir} (encrypted); Ctrl-C to stop", flush=True)
    else:
        print("Ctrl-C to stop", flush=True)
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        host.stop()
        print("\nshutting down", flush=True)


def cmd_artifact_fetch(args) -> None:
    from . import artifact as A
    from .artifact_exchange import fetch
    host, _, port = args.addr.rpartition(":")
    art = fetch((host, int(port)), args.hash)
    if art is None:
        sys.exit("not found (unknown hash or unreachable host)")
    if not A.verify(art):
        sys.exit("artifact FAILED verification — refusing")
    m = art.manifest
    print(f"verified: {_sanitize(m['name'])} v{_sanitize(m['version'])} [{_sanitize(m['kind'])}] "
          f"from {m['publisher_node'][:12]}… ({m['size']} bytes)")
    if not args.install:
        print("re-run with --install DIR to extract (you'll be asked to approve).")
        return
    # The human gate: a signed artifact is still untrusted CODE — require an explicit yes.
    resp = input(f"Install '{_sanitize(m['name'])}' from {m['publisher_node'][:12]}…? [y/N] ").strip().lower()
    dest = A.install(art, args.install, approve=lambda _m: resp == "y")
    print(f"extracted to {dest}  (NOT executed — review before running)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aimessage", description="AIMessage P2P node daemon + CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run a long-lived node (peer or bootstrap)")
    s.add_argument("--listen", default="/ip4/0.0.0.0/tcp/0", help="libp2p listen multiaddr")
    s.add_argument("--bootstrap", help="multiaddr of a node to join (omit to be a bootstrap)")
    src = s.add_mutually_exclusive_group()
    src.add_argument("--centralaizer", help="Centralaizer base URL, e.g. http://127.0.0.1:3001")
    src.add_argument("--memories", help="JSON file of memories to serve")
    s.add_argument("--federate-shared", action="store_true",
                   help="With --centralaizer: treat the hub's owner=shared memories as mesh-federated "
                        "(data-egress opt-in; off by default). Prefer an explicit federation marker.")
    s.add_argument("--writeback", action="store_true",
                   help="With --centralaizer + --control: dashboard 'Ask the mesh' answers are absorbed "
                        "into this hub (Phase 7 mesh→hub write-back), so your memory_search finds them.")
    s.add_argument("--name", default="node")
    s.add_argument("--key", default=str(DEFAULT_KEY_PATH), help="identity key path (persistent)")
    s.add_argument("--lan", action="store_true",
                   help="LAN/localhost mode: disable gossipsub eclipse/spam guards (needed to mesh "
                        "same-IP peers). Leave OFF on a real WAN.")
    s.add_argument("--notify", action="store_true",
                   help="Deliver OS desktop notifications (terminal-notifier/notify-send) for inbound "
                        "questions and answers, coalesced + rate-limited.")
    s.add_argument("--notify-preview", action="store_true",
                   help="Include a truncated query preview in notifications (default: minimal body, "
                        "content stays in-app / off the lock screen).")
    s.add_argument("--control", action="store_true",
                   help="Start the localhost control API (token-gated) for a desktop/UI to observe "
                        "this node. Prints CONTROL_ADDR + CONTROL_TOKEN.")
    s.add_argument("--control-port", type=int, default=0, help="control API port (default: ephemeral)")
    s.add_argument("--trust-file", help="path to the trusted-origins allowlist (default: ~/.aimessage/trusted.json)")
    s.add_argument("--forget-poll-interval", type=float, default=60.0,
                   help="with --centralaizer: seconds between /api/forgotten polls → mesh revocations")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("ask", help="broadcast a query to the mesh and print answers")
    a.add_argument("query")
    a.add_argument("--bootstrap", required=True, help="multiaddr to join")
    a.add_argument("--window", type=float, default=4.0, help="seconds to collect answers")
    a.add_argument("--settle", type=float, default=2.5, help="seconds to let the mesh form")
    a.add_argument("--key", default=None, help="identity key path (default: ephemeral)")
    a.add_argument("--lan", action="store_true", help="LAN/localhost mode (see `serve --lan`)")
    a.add_argument("--writeback", metavar="HUB_URL", default=None,
                   help="absorb the answers into a local Centralaizer hub (Phase 7: mesh→hub "
                        "write-back), e.g. http://127.0.0.1:3001 — so your own memory_search finds "
                        "them next time. Trust-gated + PII-masked hub-side; skips locally-revoked ones.")
    a.set_defaults(func=cmd_ask)

    i = sub.add_parser("id", help="print this node's id (b64 Ed25519 public key)")
    i.add_argument("--key", default=str(DEFAULT_KEY_PATH))
    i.set_defaults(func=cmd_id)

    asv = sub.add_parser("artifact-serve", help="pack a directory into a signed artifact and serve it")
    asv.add_argument("path", help="directory to pack (a plugin / MCP-server bundle)")
    asv.add_argument("--name", required=True)
    asv.add_argument("--version", default="0.1.0")
    asv.add_argument("--kind", default="plugin", choices=["plugin", "mcp-server"])
    asv.add_argument("--description", default="")
    asv.add_argument("--port", type=int, default=0)
    asv.add_argument("--cold-dir", help="encrypted overflow dir (e.g. inside a Google Drive synced folder)")
    asv.add_argument("--key", default=str(DEFAULT_KEY_PATH))
    asv.set_defaults(func=cmd_artifact_serve)

    af = sub.add_parser("artifact-fetch", help="fetch an artifact by hash, verify, optionally install")
    af.add_argument("hash", help="content hash (sha256 hex)")
    af.add_argument("--addr", required=True, help="host:port of an artifact-serve node")
    af.add_argument("--install", help="extract into this dir after an interactive approval prompt")
    af.set_defaults(func=cmd_artifact_fetch)

    t = sub.add_parser("trust", help="manage the trusted-origins allowlist (anti-sybil ranking anchor)")
    t.add_argument("trust_cmd", choices=["add", "remove", "list"])
    t.add_argument("node_id", nargs="?", help="origin node id (b64 Ed25519 pubkey); required for add/remove")
    t.add_argument("--trust-file", help="allowlist path (default: ~/.aimessage/trusted.json)")
    t.set_defaults(func=cmd_trust)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
