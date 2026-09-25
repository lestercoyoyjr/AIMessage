"""Event bus (PE) — pub-sub, bounded buffer, and the three emit points over a real socket exchange.
Pure stdlib → runs in CI.

Runnable:  python tests/test_events.py   |   pytest
"""
import sys
from pathlib import Path

from nacl.signing import SigningKey

from aimessage.events import ANSWER_RECEIVED, ANSWER_SENT, QUERY_RECEIVED, EventBus  # noqa: E402
from aimessage.memory import Owner, PortableMemory  # noqa: E402
from aimessage.node import Node  # noqa: E402
from aimessage.store import MemoryStore  # noqa: E402


def test_emit_and_recent():
    bus = EventBus()
    bus.emit(QUERY_RECEIVED, asker="x", query="hello")
    got = bus.recent()
    assert len(got) == 1 and got[0].type == QUERY_RECEIVED and got[0].detail["query"] == "hello"


def test_subscribe_and_unsubscribe():
    bus = EventBus()
    seen = []
    unsub = bus.subscribe(seen.append)
    bus.emit(ANSWER_SENT, count=1)
    assert len(seen) == 1
    unsub()
    bus.emit(ANSWER_SENT, count=2)
    assert len(seen) == 1                     # no delivery after unsubscribe


def test_buffer_is_bounded():
    bus = EventBus(buffer_size=3)
    for i in range(10):
        bus.emit(QUERY_RECEIVED, i=i)
    assert len(bus.recent(n=100)) == 3        # ring buffer capped


def test_raising_subscriber_does_not_break_emit():
    bus = EventBus()
    good = []
    bus.subscribe(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))  # raises
    bus.subscribe(good.append)                # must still get the event
    bus.emit(QUERY_RECEIVED)
    assert len(good) == 1


def test_recent_filters_by_type():
    bus = EventBus()
    bus.emit(QUERY_RECEIVED)
    bus.emit(ANSWER_SENT, count=1)
    assert len(bus.recent(type_=ANSWER_SENT)) == 1


def test_node_emits_query_answer_events_over_socket():
    b = SigningKey.generate()
    responder_bus, asker_bus = EventBus(), EventBus()
    store = MemoryStore([PortableMemory(content="chronic care management biologic",
                                        type="semantic", owner=Owner.FEDERATED.value,
                                        trust_score=0.9).sign(b)])
    responder = Node(key=b, store=store, events=responder_bus)
    addr = responder.serve()
    try:
        asker = Node(events=asker_bus)
        hits = asker.ask(addr, "chronic care management")
        assert len(hits) == 1
        r_types = {e.type for e in responder_bus.recent()}
        assert QUERY_RECEIVED in r_types and ANSWER_SENT in r_types
        assert ANSWER_RECEIVED in {e.type for e in asker_bus.recent()}
    finally:
        responder.stop()


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
