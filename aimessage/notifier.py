"""Notification layer (PN) — consumes the event bus and delivers desktop/OS notifications.

The bus (events.py) just delivers raw events; this layer adds the policy the plan called for:
  * COALESCE + RATE-LIMIT — at most one notification per (event-type, peer) per `window_s`; bursts are
    counted and surfaced as "(+N more in the last 60s)" so a query flood → one notification, not 60.
  * TRUST-GATE — optional `trusted_peers`: inbound `query.received` from an unknown peer is dropped
    (its the notification-spam vector; answers to your own queries are always relevant).
  * MINIMAL BODY — default notification text names the peer + counts only; the memory content stays
    in-app (off the lock screen) unless `show_preview=True`, and even then it's sanitized + truncated.

Timer-free: coalescing is driven by event arrivals (a throttle with a suppressed-count), so it needs
no background task and is deterministic under an injected clock in tests.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time

from .events import ANSWER_RECEIVED, ANSWER_SENT, QUERY_RECEIVED, sanitize_text

_TITLES = {
    QUERY_RECEIVED: "New question",
    ANSWER_SENT: "You answered a peer",
    ANSWER_RECEIVED: "New answer",
}


def os_notify(title: str, body: str) -> None:
    """Best-effort desktop notification: terminal-notifier (macOS) or notify-send (Linux); else skip.
    title/body are already sanitized and passed as argv (never a shell), so peer text can't inject."""
    try:
        if sys.platform == "darwin" and shutil.which("terminal-notifier"):
            subprocess.run(["terminal-notifier", "-title", title, "-message", body],
                           check=False, timeout=5)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False, timeout=5)
    except Exception:
        pass


class Notifier:
    def __init__(self, deliver=os_notify, *, window_s: float = 60.0, types=None,
                 trusted_peers=None, show_preview: bool = False, clock=time.time):
        self._deliver = deliver
        self.window_s = window_s
        self.types = set(types) if types else {QUERY_RECEIVED, ANSWER_SENT, ANSWER_RECEIVED}
        self.trusted_peers = set(trusted_peers) if trusted_peers is not None else None
        self.show_preview = show_preview
        self._clock = clock
        self._state: dict = {}          # (type, peer) -> [last_notified_ts, suppressed_count]

    def attach(self, bus):
        """Subscribe to an EventBus; returns the unsubscribe callable."""
        return bus.subscribe(self.on_event)

    def on_event(self, ev) -> None:
        if ev.type not in self.types:
            return
        peer = ev.detail.get("asker") or ev.detail.get("peer")
        if (ev.type == QUERY_RECEIVED and self.trusted_peers is not None
                and peer not in self.trusted_peers):
            return                      # trust-gate: ignore inbound questions from unknown peers
        key = (ev.type, peer)
        now = self._clock()
        st = self._state.get(key)
        if st is None or now - st[0] >= self.window_s:
            suppressed = st[1] if st else 0
            self._state[key] = [now, 0]
            title, body = self._format(ev, peer, suppressed)
            self._deliver(title, body)
        else:
            st[1] += 1                  # within the window → suppress, count for the next fire

    def _format(self, ev, peer, suppressed: int):
        who = (str(peer)[:12] + "…") if peer else "a peer"
        title = _TITLES.get(ev.type, ev.type)
        if ev.type == QUERY_RECEIVED:
            body = f"{who} asked a question"
        elif ev.type == ANSWER_RECEIVED:
            body = f"{ev.detail.get('count', 0)} answer(s) received"
        else:  # ANSWER_SENT
            body = f"answered {who} ({ev.detail.get('count', 0)} memories)"
        if suppressed:
            body += f" (+{suppressed} more in the last {int(self.window_s)}s)"
        if self.show_preview and ev.detail.get("query"):
            body += f': "{sanitize_text(str(ev.detail["query"]))[:60]}"'
        return title, body
