"""Phase 6: broadcast discovery over gossipsub — "ask the whole mesh, whoever knows answers".

Unlike the point-to-point libp2p transport (you must know the peer), this lets a node shout a
question to everyone on a topic. Flow, reusing the same sign/seal/verify primitive unchanged:

  1. every node subscribes to QUERY_TOPIC and ANSWER_TOPIC
  2. asker publishes a signed query to QUERY_TOPIC (its nonce doubles as the correlation id)
  3. any node with a federated hit seals its answer TO THE ASKER and publishes it to ANSWER_TOPIC
  4. asker collects answers matching its nonce for a short window, decrypts + verifies + dedups

Sealed answers ride a shared topic, so every subscriber sees the ciphertext — but only the asker
can open it (SealedBox to the asker's key). Confidentiality holds without a per-query topic.

Optional dep (`requirements-libp2p.txt`). ponytail: localhost gossipsub needs the eclipse/spam
guards OFF (they refuse to graft peers sharing 127.0.0.1) and a short heartbeat — fine for a LAN/
demo; turn them back ON for a real WAN mesh where IP diversity is genuine.
"""
from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager

import multiaddr
import trio
from libp2p import new_host
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.pubsub.gossipsub import PROTOCOL_ID as GOSSIPSUB_PROTO
from libp2p.pubsub.gossipsub import GossipSub
from libp2p.pubsub.pubsub import Pubsub
from libp2p.tools.anyio_service import background_trio_service
from nacl.signing import SigningKey

from .identity import node_id
from .libp2p_transport import _keypair_from
from .protocol import (
    ReplayGuard, answer_query, make_query, open_answer_envelope, rank_corroborated, seal_to,
    verify_query,
)
from .artifact import verify_manifest
from .store import MemoryStore
from .tombstone import TombstoneLog, make_tombstone, verify_tombstone

QUERY_TOPIC = "aimessage/queries/1.0.0"
ANSWER_TOPIC = "aimessage/answers/1.0.0"
TOMBSTONE_TOPIC = "aimessage/tombstones/1.0.0"
ARTIFACT_TOPIC = "aimessage/artifacts/1.0.0"
# Shared rendezvous key: every AIMessage node advertises itself here so peers find each
# other via the DHT with only a bootstrap address — no per-peer addresses needed.
RENDEZVOUS = "aimessage:rendezvous:v1"

_PER_RESPONDER_CAP = 50     # bound how many memories one responder can inject into a result set
_TOTAL_ANSWER_CAP = 500     # bound total memories collected per broadcast (anti-firehose)


class GossipNode:
    def __init__(self, key: SigningKey | None = None,
                 store: MemoryStore | None = None, name: str = "node", lan_mode: bool = False,
                 trusted_origins=None, events=None):
        self.key = key or SigningKey.generate()
        self.store = store or MemoryStore()
        self.name = name
        self.events = events                                 # optional EventBus
        self._trusted_origins = set(trusted_origins or ())   # reputation anchor for ranking (NEW-3)
        self._host = new_host(key_pair=_keypair_from(self.key))
        # WAN-safe by default: gossipsub eclipse/spam guards ON. lan_mode turns them OFF (they reject
        # same-IP peers, so a localhost/LAN mesh can't form otherwise) and speeds the heartbeat.
        gs_kwargs = (dict(heartbeat_interval=1, eclipse_protection_enabled=False,
                          spam_protection_enabled=False) if lan_mode else {})
        self._gs = GossipSub(protocols=[GOSSIPSUB_PROTO], degree=2, degree_low=1, degree_high=4,
                             **gs_kwargs)
        self._ps = Pubsub(self._host, self._gs)
        self._dht = KadDHT(self._host, DHTMode.SERVER)
        self._guard = ReplayGuard()      # freshness + no-replay on inbound queries
        self._tombstones = TombstoneLog()  # revoked content-addresses (mesh erasure)
        self._catalog: dict[str, dict] = {}  # content_hash -> {manifest, sig, addr}: discoverable artifacts
        self._q_sub = None
        self._a_sub = None
        self._t_sub = None
        self._c_sub = None

    @property
    def node_id(self) -> str:
        return node_id(self.key)

    def addr(self) -> str:
        return str(self._host.get_addrs()[0])

    async def connect(self, peer_maddr: str) -> None:
        await self._host.connect(info_from_p2p_addr(multiaddr.Multiaddr(peer_maddr)))

    async def advertise(self) -> bool:
        """Announce this node under the shared rendezvous key so others can discover it."""
        return await self._dht.provide(RENDEZVOUS)

    async def join(self, bootstrap_maddr: str, settle: float = 3.0) -> int:
        """Bootstrap into the network, then discover + connect to peers via the rendezvous key.
        Only the bootstrap's address is needed; peers are found through the DHT. Returns the
        number of peers connected."""
        boot = info_from_p2p_addr(multiaddr.Multiaddr(bootstrap_maddr))
        await self._host.connect(boot)
        await self._dht.add_peer(boot.peer_id)
        await self.advertise()
        return await self.discover(settle=settle)

    async def discover(self, settle: float = 0.0, count: int = 32) -> int:
        """Re-query the rendezvous key and connect to any peers found. Returns peers connected.

        ponytail: py-libp2p 0.7.0's `find_providers` is unreliable on tiny nets — the asker
        direction often returns only itself, so this can return 0 even when providers exist.
        That's why `join()` always connects to the bootstrap: gossipsub relays through it, so
        the mesh is reachable regardless. On a real-scale network the DHT lookup does the work;
        here it's best-effort on top of the guaranteed bootstrap relay."""
        if settle:
            await trio.sleep(settle)  # let provider records propagate
        me = self._host.get_id()
        connected = 0
        for p in await self._dht.find_providers(RENDEZVOUS, count=count):
            if p.peer_id == me:
                continue
            try:
                await self._host.connect(p)
                connected += 1
            except Exception:  # noqa: BLE001 — unreachable/duplicate peer, skip
                continue
        return connected

    @asynccontextmanager
    async def running(self, listen: str = "/ip4/0.0.0.0/tcp/0"):
        async with self._host.run([multiaddr.Multiaddr(listen)]):
            async with background_trio_service(self._gs), background_trio_service(self._ps), \
                       background_trio_service(self._dht):
                self._q_sub = await self._ps.subscribe(QUERY_TOPIC)
                self._a_sub = await self._ps.subscribe(ANSWER_TOPIC)
                self._t_sub = await self._ps.subscribe(TOMBSTONE_TOPIC)
                self._c_sub = await self._ps.subscribe(ARTIFACT_TOPIC)
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(self._serve_queries)
                    nursery.start_soon(self._serve_tombstones)
                    nursery.start_soon(self._serve_catalog)
                    try:
                        yield self
                    finally:
                        nursery.cancel_scope.cancel()

    def _apply_tombstone(self, t: dict) -> None:
        """Record a verified revocation and drop any locally-held copy that ACTUALLY came from the
        revoker (authorship-bound authority — you can't erase someone else's memory)."""
        target, revoker = t["target"], t["revoker"]
        self._tombstones.record(target, revoker)
        if isinstance(self.store, MemoryStore):
            self.store.memories = [m for m in self.store.memories
                                   if not (m.id == target and m.origin_node == revoker)]
        if self.events is not None:
            self.events.emit("tombstone.received", target=target, revoker=revoker)

    async def advertise_artifact(self, manifest: dict, sig: str, addr: str = "") -> None:
        """Broadcast that an artifact exists (manifest + publisher sig + where to fetch it)."""
        await self._ps.publish(ARTIFACT_TOPIC, json.dumps(
            {"type": "artifact-advert", "manifest": manifest, "sig": sig, "addr": addr}).encode())

    async def _serve_catalog(self) -> None:
        while True:
            msg = await self._c_sub.get()
            try:
                adv = json.loads(msg.data)
                manifest, sig = adv["manifest"], adv["sig"]
            except (ValueError, TypeError, KeyError):
                continue
            # Only catalog artifacts whose manifest is validly signed by its publisher — a peer can't
            # poison the catalog with a fake manifest. (The `addr` is an untrusted hint; a bad one just
            # fails to fetch, and any fetched blob is re-checked by content-address anyway.)
            if not isinstance(manifest, dict) or not verify_manifest(manifest, sig):
                continue
            chash = manifest.get("content_hash")
            if isinstance(chash, str):
                self._catalog[chash] = {"manifest": manifest, "sig": sig,
                                        "addr": adv.get("addr", "")}

    def known_artifacts(self) -> list[dict]:
        """The discovered catalog: [{manifest, sig, addr}, ...] of verified artifacts heard on the mesh."""
        return list(self._catalog.values())

    async def _serve_tombstones(self) -> None:
        while True:
            msg = await self._t_sub.get()
            try:
                t = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if verify_tombstone(t):                 # signed by its declared revoker
                self._apply_tombstone(t)

    async def revoke(self, memory) -> None:
        """Revoke one of THIS node's own memories mesh-wide: gossip a signed tombstone + drop locally."""
        if memory.origin_node != self.node_id:
            raise ValueError("a node can only revoke memories it originated")
        await self.revoke_hash(memory.id)

    async def revoke_hash(self, content_hash: str) -> None:
        """Revoke a content-address this node federated (signed with its own key), by hash. Used by the
        Centralaizer forgotten-poller: the hub erased memory X → tombstone its federated copy mesh-wide."""
        t = make_tombstone(content_hash, self.key)
        await self._ps.publish(TOMBSTONE_TOPIC, json.dumps(t).encode())
        self._apply_tombstone(t)

    async def _serve_queries(self) -> None:
        while True:
            msg = await self._q_sub.get()
            try:
                q = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if not isinstance(q, dict):              # a non-object query can't be valid; don't .get() it
                continue
            if q.get("asker_node") == self.node_id or not verify_query(q):
                continue
            if not self._guard.check(q):            # reject stale / replayed queries (anti-amplification)
                continue
            if self.events is not None:
                self.events.emit("query.received", asker=q.get("asker_node"), query=q.get("query"))
            hits = [h for h in self.store.search(q["query"])
                    if not self._tombstones.is_revoked(h.id)]   # never re-serve a revoked memory
            if not hits:                            # only speak up if we actually have something
                continue
            sealed = answer_query(q, self.store, self.key, hits=hits)  # single search, sealed to asker
            env = json.dumps({"q": q["nonce"],
                              "sealed": base64.b64encode(sealed).decode()}).encode()
            await self._ps.publish(ANSWER_TOPIC, env)
            if self.events is not None:
                self.events.emit("answer.sent", asker=q.get("asker_node"), count=len(hits))

    async def broadcast_ask(self, text: str, window: float = 3.0) -> list:
        """Shout a query to the mesh; gather verified, decrypted answers for `window` s.

        Ranked by CORROBORATION (how many distinct, authenticated responders returned the same
        content-addressed memory) — NOT by the wire `trust_score`, which any peer can set to 1.0.
        Bounded per-responder and in total so a firehose can't exhaust memory.
        """
        q = make_query(text, self.key)
        nonce = q["nonce"]
        await self._ps.publish(QUERY_TOPIC, json.dumps(q).encode())
        pairs: list = []                       # (responder_node, memory), bounded
        per_responder: dict[str, int] = {}
        with trio.move_on_after(window):
            while True:
                msg = await self._a_sub.get()
                # Total helper: any malformed/hostile answer → (None, []) (never raises).
                responder, mems = open_answer_envelope(msg.data, nonce, self.key)
                if responder is None:
                    continue
                for m in mems:
                    if per_responder.get(responder, 0) >= _PER_RESPONDER_CAP:
                        break
                    per_responder[responder] = per_responder.get(responder, 0) + 1
                    pairs.append((responder, m))
                    if len(pairs) >= _TOTAL_ANSWER_CAP:
                        break
                if len(pairs) >= _TOTAL_ANSWER_CAP:
                    break
        # Rank by distinct-origin corroboration (sybil-limited; see rank_corroborated), reputation-
        # weighted by any trusted origins — NOT by the attacker-settable wire trust_score.
        ranked = rank_corroborated(pairs, self._trusted_origins)
        if self.events is not None and ranked:
            self.events.emit("answer.received", count=len(ranked))
        return ranked
