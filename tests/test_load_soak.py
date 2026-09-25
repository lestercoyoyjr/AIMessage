"""Load / soak — sustained firehose + long-running memory bounds. Pure (no libp2p), deterministic
(ReplayGuard fed with a controlled clock, so no flaky timing), CI-safe.

Proves the node survives abuse without unbounded growth: the replay guard, tombstone log, and artifact
cache all stay bounded under a firehose, the guard fails CLOSED (never OOMs) and self-heals as time
advances, and the end-to-end handle_query path holds up at volume.

Runnable:  python tests/test_load_soak.py   |   pytest
"""
import tracemalloc

from nacl.signing import SigningKey

from aimessage import artifact as A
from aimessage.protocol import (
    ReplayGuard, handle_query, make_query, rank_corroborated,
)
from aimessage.memory import Owner, PortableMemory
from aimessage.storage import ArtifactCache
from aimessage.store import MemoryStore
from aimessage.tombstone import TombstoneLog


def _fed(content, key):
    return PortableMemory(content=content, type="semantic",
                          owner=Owner.FEDERATED.value, trust_score=0.8).sign(key)


# ── ReplayGuard: the anti-amplification structure that a firehose targets ────────────
def test_replay_guard_firehose_fails_closed_bounded():
    # A flood of DISTINCT fresh nonces at one instant must never grow _seen past max_entries;
    # once full of fresh entries the guard fails CLOSED (rejects) rather than allocating forever.
    MAX = 2000
    g = ReplayGuard(window_s=30.0, clock=lambda: 1000.0, max_entries=MAX)
    accepted = rejected = 0
    peak = 0
    for i in range(20 * MAX):                         # 40k queries, one fixed instant
        ok = g.check({"nonce": f"n{i}", "ts": 1000.0})
        accepted += ok
        rejected += not ok
        peak = max(peak, len(g._seen))
    assert peak <= MAX, f"_seen grew to {peak} > cap {MAX}"
    assert accepted == MAX and rejected == 20 * MAX - MAX   # exactly cap accepted, rest fail closed
    assert g.check({"nonce": "n0", "ts": 1000.0}) is False  # still rejecting (full + n0 already seen)


def test_replay_guard_advancing_clock_self_heals_bounded():
    # The soak: sustained load over time. As the clock advances past the window, stale nonces prune,
    # so fresh queries keep being accepted forever AND memory stays bounded — no permanent lockout.
    MAX = 2000
    now = {"t": 0.0}
    g = ReplayGuard(window_s=30.0, clock=lambda: now["t"], max_entries=MAX)
    accepted = 0
    peak = 0
    for i in range(40_000):
        now["t"] += 0.1                              # 0.1s between queries → window holds ~300
        accepted += g.check({"nonce": f"n{i}", "ts": now["t"]})
        peak = max(peak, len(g._seen))
    assert peak <= MAX, f"_seen grew to {peak} > cap {MAX}"
    assert accepted == 40_000, "advancing-clock stream must never be wrongly rejected"
    # window ~30s at 0.1s spacing → steady-state well under the cap, so it self-healed by pruning
    assert len(g._seen) < MAX


def test_replay_guard_never_re_accepts_a_replay_under_load():
    g = ReplayGuard(window_s=1e9, clock=lambda: 5.0, max_entries=100_000)
    for i in range(10_000):
        assert g.check({"nonce": f"n{i}", "ts": 5.0}) is True
    for i in range(10_000):
        assert g.check({"nonce": f"n{i}", "ts": 5.0}) is False   # every one is a replay now


# ── TombstoneLog: revocations accumulate over a long-running node ────────────────────
def test_tombstone_log_bounded_under_churn():
    MAX = 1000
    log = TombstoneLog(max_entries=MAX)
    for i in range(50 * MAX):
        log.record(f"hash{i}", "revoker")
    assert len(log._revoked) <= MAX
    assert log.is_revoked(f"hash{50*MAX-1}")          # newest kept
    assert not log.is_revoked("hash0")                # oldest evicted


# ── ArtifactCache: sustained artifact churn over quota ──────────────────────────────
def test_artifact_cache_churn_stays_within_quota():
    k = SigningKey.generate()
    one = A.pack({"f": b"x" * 1000}, name="probe", kind="plugin", version="1.0", key=k)
    quota = len(one.blob) * 10                          # room for ~10
    c = ArtifactCache(quota_bytes=quota)
    last = None
    for i in range(500):
        art = A.pack({"f": bytes([i % 256]) * 1000}, name=f"a{i}", kind="plugin", version="1.0", key=k)
        c.add(art)
        last = art
        assert c.total_bytes <= quota, f"over quota at {i}: {c.total_bytes} > {quota}"
    assert last.content_hash in c                       # most-recent survives
    assert len(c) <= 11


# ── rank_corroborated: a firehose of answers collapses to distinct content ───────────
def test_rank_corroborated_bounded_by_distinct_content():
    keys = [SigningKey.generate() for _ in range(5)]
    contents = [f"fact {j}" for j in range(8)]
    mems = {(j, ki): _fed(contents[j], keys[ki]) for j in range(8) for ki in range(5)}
    pairs = []
    for i in range(20_000):
        j, ki = i % 8, (i // 8) % 5
        pairs.append((f"responder{i%37}", mems[(j, ki)]))
    ranked = rank_corroborated(pairs)
    assert len(ranked) == 8                             # output = distinct contents, NOT 20k inputs


# ── End-to-end handle_query at volume ───────────────────────────────────────────────
def test_handle_query_firehose_end_to_end():
    import json
    responder = SigningKey.generate()
    store = MemoryStore([_fed("chronic care management program", responder)])
    guard = ReplayGuard(window_s=1e9, max_entries=100_000)   # wide window: all fresh queries accepted
    asker = SigningKey.generate()
    answered = 0
    for _ in range(3000):
        q = make_query("chronic care management", asker)     # fresh nonce + ts each call
        sealed = handle_query(json.dumps(q).encode(), store, responder, guard=guard)
        answered += bool(sealed)
    assert answered == 3000, "every fresh, valid query must be answered"
    assert len(guard._seen) == 3000                    # each distinct nonce recorded once


def test_no_unbounded_growth_soak():
    # Guard the guard: 100k queries through a bounded ReplayGuard must not grow traced memory without
    # bound. Since _seen is capped, peak allocation stays flat regardless of stream length.
    MAX = 2000
    now = {"t": 0.0}
    g = ReplayGuard(window_s=30.0, clock=lambda: now["t"], max_entries=MAX)
    tracemalloc.start()
    for i in range(100_000):
        now["t"] += 0.1
        g.check({"nonce": f"n{i}", "ts": now["t"]})
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(g._seen) <= MAX
    assert peak < 32 * 1024 * 1024, f"peak traced memory {peak/1e6:.1f}MB too high for a bounded guard"


if __name__ == "__main__":
    import time as _t
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        t0 = _t.time()
        fn()
        print(f"ok  {name}  ({_t.time()-t0:.1f}s)")
    print(f"\nall {len(tests)} passed")
