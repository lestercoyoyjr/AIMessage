"""The Phase 3 primitive: signed query in, sealed + signed answer out.

Wire messages are JSON. Security properties:
  * Authenticity — the query is Ed25519-signed by the asker; the answer is Ed25519-signed by the
    responder; every returned memory is signed by its origin. Receivers verify all three.
  * Freshness / no-replay — queries carry a signed `ts`; a `ReplayGuard` rejects stale queries and
    repeated nonces (assumes peers' clocks are within the window; fine on a LAN, generous on a WAN).
  * Binding — the asker's query nonce is sealed INSIDE the answer, so a captured answer can't be
    transplanted onto a different query.
  * Confidentiality (E2E) — the answer is sealed to the asker's key with a libsodium SealedBox.
  * Domain separation — query and answer signatures are tagged so one can't be replayed as the other.

PHI never rides the wire: memory `content` is masked upstream (Centralaizer) before a PortableMemory
exists (see memory.py invariant). This layer signs and seals; it never unmasks.
"""
from __future__ import annotations

import base64
import json
import os
import time

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

from .memory import PortableMemory
from .store import MemoryStore

QUERY_TAG = b"aimessage/query/v1:"          # domain separation between signed message types
ANSWER_TAG = b"aimessage/answer/v1:"
_QUERY_SIGNED_FIELDS = ("asker_node", "nonce", "query", "ts", "type")
_ANSWER_SIGNED_FIELDS = ("memories", "query_nonce", "responder_node", "type")


def _node_id(key: SigningKey) -> str:
    return base64.b64encode(bytes(key.verify_key)).decode()


def _canonical(msg: dict, fields) -> bytes:
    return json.dumps({k: msg[k] for k in fields}, sort_keys=True,
                      separators=(",", ":")).encode()


# --- replay / freshness -------------------------------------------------------

class ReplayGuard:
    """Rejects stale or replayed queries: a query must carry a `ts` within `window_s` of now and a
    `nonce` not seen before. Inject `clock` in tests to avoid real sleeps.

    Memory is HARD-bounded to `max_entries` (NEW-1): the seen-nonce set is pruned of entries outside
    the freshness window, and if it is still full a new query is REJECTED (fail closed) rather than
    growing without limit. Under a flood this trades availability (bounded, self-healing within
    `window_s`) for integrity + bounded memory — the right call for a replay guard.

    Entries are keyed on the query's `ts`, the SAME clock domain as the freshness check (NEW-2), so an
    entry is pruned exactly when a query bearing it would already fail freshness — no window in which a
    nonce is pruned while still fresh (which would let a replay through)."""

    def __init__(self, window_s: float = 30.0, clock=time.time, max_entries: int = 50_000):
        self.window_s = window_s
        self._clock = clock
        self._max = max_entries
        self._seen: dict[str, float] = {}        # nonce -> query ts

    def check(self, msg: dict) -> bool:
        now = self._clock()
        ts, nonce = msg.get("ts"), msg.get("nonce")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(nonce, str):
            return False
        if abs(now - ts) > self.window_s:            # too old, or implausibly far in the future
            return False
        if nonce in self._seen:
            return False                             # replay (checked before pruning)
        if len(self._seen) >= self._max:             # at capacity → prune stale, keyed on ts
            cutoff = now - self.window_s
            self._seen = {n: t for n, t in self._seen.items() if t > cutoff}
            if len(self._seen) >= self._max:         # still full of fresh entries → fail closed
                return False
        self._seen[nonce] = ts
        return True


# --- query --------------------------------------------------------------------

def make_query(text: str, key: SigningKey) -> dict:
    msg = {
        "type": "query",
        "query": text,
        "asker_node": _node_id(key),
        "nonce": base64.b64encode(os.urandom(16)).decode(),
        "ts": round(time.time(), 3),
    }
    msg["sig"] = base64.b64encode(
        key.sign(QUERY_TAG + _canonical(msg, _QUERY_SIGNED_FIELDS)).signature
    ).decode()
    return msg


def verify_query(msg: dict) -> bool:
    try:
        if not isinstance(msg, dict) or msg.get("type") != "query":
            return False
        vk = VerifyKey(base64.b64decode(msg["asker_node"]))
        vk.verify(QUERY_TAG + _canonical(msg, _QUERY_SIGNED_FIELDS), base64.b64decode(msg["sig"]))
        return True
    except Exception:
        return False


# --- E2E seal/open (Ed25519 identity → Curve25519 for encryption) -------------

def _curve_pub(node_id_b64: str) -> PublicKey:
    return VerifyKey(base64.b64decode(node_id_b64)).to_curve25519_public_key()


def _curve_priv(key: SigningKey) -> PrivateKey:
    return key.to_curve25519_private_key()


def seal_to(node_id_b64: str, plaintext: bytes) -> bytes:
    return bytes(SealedBox(_curve_pub(node_id_b64)).encrypt(plaintext))


def open_sealed(key: SigningKey, sealed: bytes) -> bytes:
    return SealedBox(_curve_priv(key)).decrypt(sealed)


# --- answer -------------------------------------------------------------------

def answer_query(msg: dict, store: MemoryStore, key: SigningKey, hits=None) -> bytes:
    """Build the answer to `msg` from the local federated store, SIGNED by us and SEALED to the asker.
    `hits` may be passed to avoid a second store search (the caller already searched)."""
    if hits is None:
        hits = store.search(msg["query"])
    payload = {
        "type": "answer",
        "responder_node": _node_id(key),
        "query_nonce": msg.get("nonce"),                 # binds the answer to THIS query
        "memories": [m.to_json() for m in hits],
    }
    payload["sig"] = base64.b64encode(
        key.sign(ANSWER_TAG + _canonical(payload, _ANSWER_SIGNED_FIELDS)).signature
    ).decode()
    return seal_to(msg["asker_node"], json.dumps(payload).encode())


def _open_answer_payload(sealed: bytes, key: SigningKey, expected_nonce):
    """Decrypt + fully validate an answer → (responder_node | None, verified_memories).
    Anything hostile/malformed → (None, []). Never raises."""
    if not sealed:
        return (None, [])
    try:
        raw = open_sealed(key, sealed)
    except (CryptoError, ValueError):
        return (None, [])                                # not sealed to us / corrupt ciphertext
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return (None, [])
    if not isinstance(data, dict):
        return (None, [])
    if expected_nonce is not None and data.get("query_nonce") != expected_nonce:
        return (None, [])                                # answer transplanted onto another query
    responder = data.get("responder_node")
    try:                                                 # authenticate the responder attribution
        VerifyKey(base64.b64decode(responder)).verify(
            ANSWER_TAG + _canonical(data, _ANSWER_SIGNED_FIELDS), base64.b64decode(data["sig"]))
    except Exception:
        return (None, [])                                # unsigned / forged responder
    blobs = data.get("memories", [])
    if not isinstance(blobs, list):
        return (responder, [])
    out: list[PortableMemory] = []
    for j in blobs:
        try:
            m = PortableMemory.from_json(j)
        except (ValueError, TypeError):
            continue
        if m.verify():
            out.append(m)
    return (responder, out)


def open_answer(sealed: bytes, key: SigningKey, expected_nonce=None) -> list[PortableMemory]:
    """Decrypt an answer and return only memories whose signatures verify (responder authenticated,
    query-nonce bound when `expected_nonce` is given). Guarded: hostile input → []."""
    return _open_answer_payload(sealed, key, expected_nonce)[1]


def open_answer_envelope(raw: bytes, expected_nonce: str, key: SigningKey):
    """Parse a gossip answer envelope `{q, sealed}` from an untrusted peer.
    → (responder_node | None, verified_memories). Returns (None, []) on ANYTHING malformed."""
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return (None, [])
    if not isinstance(env, dict) or env.get("q") != expected_nonce:
        return (None, [])
    try:
        sealed = base64.b64decode(env.get("sealed", ""))   # binascii.Error subclasses ValueError
    except (ValueError, TypeError):
        return (None, [])
    return _open_answer_payload(sealed, key, expected_nonce)


def rank_corroborated(pairs, trusted_origins=None) -> list[PortableMemory]:
    """Rank collected answers by (a memory of a trusted origin, then) how many DISTINCT ORIGINS
    independently asserted the same content.

    `pairs`: list of (responder_node, PortableMemory). Grouping is by `content`, and corroboration is
    the count of distinct `origin_node`s in a group.

    Sybil note (NEW-3): counting distinct ORIGINS (not relayers) defeats the cheap attack — a peer
    re-broadcasting/relaying ONE origin's memory under many responder identities counts as corroboration
    1, not N. It is NOT resistant to an attacker MINTING many origin keys and signing the same content
    with each; identity is free in a permissionless mesh, so ranking is advisory. Pass `trusted_origins`
    (a set of trusted origin_node ids) for reputation-weighted ranking — the real anchor against sybils.
    """
    trusted = trusted_origins or set()
    groups: dict[str, dict] = {}
    for _responder, m in pairs:
        g = groups.setdefault(m.content, {"mem": m, "origins": set()})
        g["origins"].add(m.origin_node)

    def score(g):
        return (any(o in trusted for o in g["origins"]), len(g["origins"]))

    return [g["mem"] for g in sorted(groups.values(), key=score, reverse=True)]


def handle_query(raw: bytes, store: MemoryStore, key: SigningKey,
                 guard: ReplayGuard | None = None, events=None) -> bytes:
    """Transport-agnostic request handler: raw bytes → sealed answer bytes. Forged/garbage/stale/
    replayed queries get b"". Pass a `guard` to enforce freshness + no-replay, and an `events` bus
    to emit query.received / answer.sent (single store search reused for both the answer and the count)."""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return b""
    if not isinstance(msg, dict) or not verify_query(msg):
        return b""
    if guard is not None and not guard.check(msg):
        return b""                                       # stale or replayed
    if events is not None:
        events.emit("query.received", asker=msg.get("asker_node"), query=msg.get("query"))
    hits = store.search(msg["query"])
    sealed = answer_query(msg, store, key, hits=hits)
    if events is not None and hits:
        events.emit("answer.sent", asker=msg.get("asker_node"), count=len(hits))
    return sealed
