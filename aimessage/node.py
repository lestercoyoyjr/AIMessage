"""A node = identity + a federated store + the ability to serve and ask queries.

This is the seam Centralaizer plugs into: swap `MemoryStore` for a Centralaizer-backed
store (real masking + vector/FTS/graph search) and the wire behavior is unchanged.
"""
from __future__ import annotations

from nacl.signing import SigningKey

from .identity import node_id
from .protocol import ReplayGuard, handle_query, make_query, open_answer
from .store import MemoryStore
from .transport import run_server, send_request


class Node:
    def __init__(self, key: SigningKey | None = None,
                 store: MemoryStore | None = None, name: str = "node", events=None):
        self.key = key or SigningKey.generate()
        self.store = store or MemoryStore()
        self.name = name
        self.events = events             # optional EventBus
        self._guard = ReplayGuard()      # freshness + no-replay on inbound queries
        self._srv = None

    @classmethod
    def with_centralaizer(cls, base_url: str | None = None, name: str = "hub",
                          allow_shared_as_federated: bool = False):
        """A node backed by a running Centralaizer hub (persistent identity + real masked/semantic
        search). `allow_shared_as_federated` must be opted into for the hub to federate anything (M6)."""
        from .centralaizer_store import DEFAULT_BASE_URL, CentralaizerStore
        from .identity import load_or_create
        key = load_or_create()
        store = CentralaizerStore(key, base_url or DEFAULT_BASE_URL,
                                  allow_shared_as_federated=allow_shared_as_federated)
        return cls(key=key, store=store, name=name)

    @property
    def node_id(self) -> str:
        return node_id(self.key)

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        self._srv = run_server(host, port, self._handle)
        return self._srv.server_address  # (host, actual_port)

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()            # release the listening socket fd (was leaked)
            self._srv = None

    def _handle(self, raw: bytes) -> bytes:
        return handle_query(raw, self.store, self.key, self._guard, self.events)

    def ask(self, addr: tuple[str, int], text: str):
        """Send a signed query to a peer; return verified, decrypted memories bound to this query."""
        import json
        q = make_query(text, self.key)
        sealed = send_request(addr[0], addr[1], json.dumps(q).encode())
        hits = open_answer(sealed, self.key, expected_nonce=q["nonce"])
        if self.events is not None and hits:
            self.events.emit("answer.received", peer=f"{addr[0]}:{addr[1]}", count=len(hits))
        return hits
