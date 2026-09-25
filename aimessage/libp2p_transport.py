"""Real P2P transport: a libp2p host in place of the localhost socket.

Same query primitive as the socket path — it reuses `protocol.handle_query` / `make_query`
/ `open_answer` untouched — but over a genuine libp2p stream: Noise-encrypted, addressed by
multiaddr + PeerID, dialable across machines/NAT. The node's PeerID derives from the SAME
Ed25519 identity key used for signing (one identity, not two).

Optional dependency: `pip install -r requirements-libp2p.txt`. This module (and trio) are only
imported when you actually use libp2p; the socket Node path needs neither.

ponytail: point-to-point request/reply over a stream is all the demo primitive needs. Gossipsub
fan-out ("broadcast to a topic, whoever knows answers") and Kademlia discovery are Phase 6 —
they layer on the same host without touching the protocol layer.
"""
from __future__ import annotations

import json
import struct

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from nacl.signing import SigningKey

from .protocol import ReplayGuard, handle_query, make_query, open_answer
from .store import MemoryStore
from .transport import MAX_FRAME

PROTOCOL_ID = "/aimessage/query/1.0.0"
_EPHEMERAL = "/ip4/127.0.0.1/tcp/0"


def _keypair_from(key: SigningKey):
    # 32-byte Ed25519 seed → deterministic libp2p KeyPair, so PeerID == signing identity.
    return create_new_key_pair(bytes(key))


async def _read_exactly(stream, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = await stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


async def _read_frame(stream, max_bytes: int = MAX_FRAME) -> bytes:
    hdr = await _read_exactly(stream, 4)
    if len(hdr) < 4:
        return b""
    (ln,) = struct.unpack("!I", hdr)
    if ln > max_bytes:
        return b""                       # refuse oversized frame without allocating (M1)
    return await _read_exactly(stream, ln)


async def _write_frame(stream, data: bytes) -> None:
    await stream.write(struct.pack("!I", len(data)) + data)


class Libp2pNode:
    def __init__(self, key: SigningKey | None = None,
                 store: MemoryStore | None = None, name: str = "node", events=None):
        self.key = key or SigningKey.generate()
        self.store = store or MemoryStore()
        self.name = name
        self.events = events             # optional EventBus
        self._guard = ReplayGuard()      # freshness + no-replay on inbound queries
        self._host = new_host(key_pair=_keypair_from(self.key))

    async def _stream_handler(self, stream) -> None:
        raw = await _read_frame(stream)
        await _write_frame(stream, handle_query(raw, self.store, self.key, self._guard, self.events))
        await stream.close()

    async def serve(self, on_ready, listen: str = "/ip4/0.0.0.0/tcp/0") -> None:
        """Listen forever. Calls `await on_ready(multiaddr_str)` once bound. Cancel to stop."""
        async with self._host.run([multiaddr.Multiaddr(listen)]):
            self._host.set_stream_handler(PROTOCOL_ID, self._stream_handler)
            await on_ready(str(self._host.get_addrs()[0]))
            await trio.sleep_forever()

    async def ask(self, peer_maddr: str, text: str) -> list:
        """Dial a peer by multiaddr, send a signed query, return verified decrypted memories."""
        info = info_from_p2p_addr(multiaddr.Multiaddr(peer_maddr))
        async with self._host.run([multiaddr.Multiaddr(_EPHEMERAL)]):
            await self._host.connect(info)
            stream = await self._host.new_stream(info.peer_id, [PROTOCOL_ID])
            q = make_query(text, self.key)
            await _write_frame(stream, json.dumps(q).encode())
            reply = await _read_frame(stream)
            await stream.close()
            hits = open_answer(reply, self.key, expected_nonce=q["nonce"])
            if self.events is not None and hits:
                self.events.emit("answer.received", peer=peer_maddr, count=len(hits))
            return hits
