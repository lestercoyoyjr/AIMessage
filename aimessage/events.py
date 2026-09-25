"""In-process event bus — the seam notifications and the desktop UI both consume.

A node emits events at the existing flow points (a peer messaged us; we answered; we got an answer)
so a consumer can react without polling. This is deliberately minimal: a thread-safe pub-sub with a
bounded, NON-persisted ring buffer. Coalescing / rate-limiting / trust-gating live in the notification
layer on top — the bus just delivers.

PRIVACY: the buffer records inbound-peer activity (who asked what), so it lives ONLY in memory, is
capped, and is never written to disk. `recent()` is for a live UI, not an audit log.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# All C0 controls (incl. \t \r \n), DEL, and C1 (0x80-0x9F). Shared by the CLI log and the notifier:
# peer-supplied text (a query) must render as a single control-free line — no ANSI, no forged lines.
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_text(s) -> str:
    """Render untrusted (peer-supplied) text as a single control-free line."""
    return _CTRL.sub(" ", s) if isinstance(s, str) else str(s)

# Event types. answer.* is split per the product decision: answer.sent = we replied to a peer;
# answer.received = a reply to our own query arrived.
QUERY_RECEIVED = "query.received"
ANSWER_SENT = "answer.sent"
ANSWER_RECEIVED = "answer.received"
ARTIFACT_OFFERED = "artifact.offered"        # reserved for Phase 4 catalog
TOMBSTONE_RECEIVED = "tombstone.received"    # reserved for Phase 5 erasure
STORAGE_FULL = "storage.full"                # hot + cold both full; an artifact was dropped


@dataclass
class Event:
    type: str
    ts: float
    detail: dict = field(default_factory=dict)


class EventBus:
    def __init__(self, buffer_size: int = 256, clock=time.time):
        self._buf: deque[Event] = deque(maxlen=buffer_size)   # bounded → memory can't grow unbounded
        self._subs: list = []
        self._lock = threading.Lock()
        self._clock = clock

    def emit(self, type_: str, **detail) -> Event:
        ev = Event(type=type_, ts=round(self._clock(), 3), detail=detail)
        with self._lock:
            self._buf.append(ev)
            subs = list(self._subs)                    # snapshot; deliver outside the lock
        for cb in subs:
            try:
                cb(ev)
            except Exception:                          # a slow/raising subscriber can't break emit
                pass                                   # ...or starve the other subscribers
        return ev

    def subscribe(self, callback):
        """Register callback(Event). Returns an unsubscribe() callable. Keep callbacks fast."""
        with self._lock:
            self._subs.append(callback)

        def _unsub():
            with self._lock:
                if callback in self._subs:
                    self._subs.remove(callback)
        return _unsub

    def recent(self, n: int = 50, type_: str | None = None) -> list[Event]:
        with self._lock:
            items = list(self._buf)
        if type_ is not None:
            items = [e for e in items if e.type == type_]
        return items[-n:]
