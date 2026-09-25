"""A tiny local memory store with naive search — enough to drive the Phase 3 demo.

`search` only ever returns FEDERATED memories: the owner gate is enforced at the
point knowledge would leave the node, so a peer can never pull a personal/shared record.

ponytail: token-overlap ranking, not semantic. Real retrieval (vector + FTS + graph)
is Centralaizer's job; this node delegates to it once Phase 3 wires the query path in.
"""
from __future__ import annotations

import re

from .memory import PortableMemory

_WORD = re.compile(r"[a-z0-9]{3,}")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class MemoryStore:
    def __init__(self, memories: list[PortableMemory] | None = None):
        self.memories: list[PortableMemory] = list(memories or [])

    def add(self, m: PortableMemory) -> None:
        self.memories.append(m)

    def search(self, query: str, limit: int = 5) -> list[PortableMemory]:
        q = _tokens(query)
        scored: list[tuple[int, float, PortableMemory]] = []
        for m in self.memories:
            if not m.is_shareable:          # owner gate — personal/shared never leave
                continue
            overlap = len(q & _tokens(m.content))
            if overlap:
                scored.append((overlap, m.trust_score, m))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [m for _, _, m in scored[:limit]]
