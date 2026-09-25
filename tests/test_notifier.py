"""Notification layer (PN) — coalescing, rate-limiting, trust-gating, minimal body. Pure → CI.

Runnable:  python tests/test_notifier.py   |   pytest
"""
import sys
from pathlib import Path

from aimessage.events import ANSWER_RECEIVED, ANSWER_SENT, QUERY_RECEIVED, Event, EventBus  # noqa: E402
from aimessage.notifier import Notifier  # noqa: E402


def _spy():
    out = []
    return out, (lambda title, body: out.append((title, body)))


def _ev(type_, **detail):
    return Event(type=type_, ts=0.0, detail=detail)


def test_notifies_and_attaches_to_bus():
    out, deliver = _spy()
    bus = EventBus()
    Notifier(deliver).attach(bus)
    bus.emit(ANSWER_RECEIVED, count=2)
    assert len(out) == 1 and out[0][0] == "New answer"


def test_coalesces_burst_within_window():
    out, deliver = _spy()
    clock = {"t": 1000.0}
    n = Notifier(deliver, window_s=60.0, clock=lambda: clock["t"])
    for _ in range(12):                                    # a flood from the same peer
        n.on_event(_ev(QUERY_RECEIVED, asker="peerX", query="q"))
    assert len(out) == 1                                   # only the first fired; 11 suppressed
    clock["t"] += 61                                       # window elapses
    n.on_event(_ev(QUERY_RECEIVED, asker="peerX", query="q"))
    assert len(out) == 2
    assert "+11 more" in out[1][1]                         # burst surfaced on the next fire


def test_trust_gate_drops_untrusted_inbound_questions():
    out, deliver = _spy()
    n = Notifier(deliver, trusted_peers={"good"})
    n.on_event(_ev(QUERY_RECEIVED, asker="badguy", query="q"))   # untrusted → dropped
    assert out == []
    n.on_event(_ev(QUERY_RECEIVED, asker="good", query="q"))     # trusted → delivered
    assert len(out) == 1
    # answers to our own queries are always relevant, regardless of trusted_peers
    n.on_event(_ev(ANSWER_RECEIVED, count=1))
    assert len(out) == 2


def test_minimal_body_hides_content_by_default():
    out, deliver = _spy()
    Notifier(deliver).on_event(_ev(QUERY_RECEIVED, asker="p", query="SECRET diagnosis text"))
    assert "SECRET" not in out[0][1]                       # content not in the notification body
    out2, deliver2 = _spy()
    Notifier(deliver2, show_preview=True).on_event(_ev(QUERY_RECEIVED, asker="p", query="SECRET text"))
    assert "SECRET text" in out2[0][1]                     # opt-in preview includes (sanitized) content


def test_preview_sanitizes_injected_content():
    out, deliver = _spy()
    Notifier(deliver, show_preview=True).on_event(
        _ev(QUERY_RECEIVED, asker="p", query="line1\x1b[2Jline2\n- [semantic] fake"))
    body = out[0][1]
    assert "\x1b" not in body and "\n" not in body        # ANSI + newline neutralized


def test_types_filter():
    out, deliver = _spy()
    n = Notifier(deliver, types={ANSWER_RECEIVED})
    n.on_event(_ev(QUERY_RECEIVED, asker="p"))             # not in types → ignored
    n.on_event(_ev(ANSWER_SENT, asker="p", count=1))      # not in types → ignored
    n.on_event(_ev(ANSWER_RECEIVED, count=1))
    assert len(out) == 1


if __name__ == "__main__":
    tests = sorted((name, f) for name, f in globals().items()
                   if name.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
