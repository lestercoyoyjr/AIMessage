"""Control API (Desktop v1.0 backend) — localhost bind + per-launch token gate. Pure stdlib → CI.

Runnable:  python tests/test_control.py   |   pytest
"""
import http.client
import json

from aimessage.control import serve_control
from aimessage.events import EventBus
from aimessage.storage import ArtifactCache


def _get(addr, path, token=None):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=3)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


def test_requires_valid_token():
    srv, token = serve_control(node_id="abc", events=EventBus())
    try:
        addr = srv.server_address
        assert _get(addr, "/status")[0] == 401             # no token
        assert _get(addr, "/status", "wrong-token")[0] == 401
        status, body = _get(addr, "/status", token)
        assert status == 200 and json.loads(body)["node_id"] == "abc"
    finally:
        srv.shutdown()
        srv.server_close()


def test_binds_localhost_only():
    srv, _token = serve_control(node_id="n")
    try:
        assert srv.server_address[0] == "127.0.0.1"        # off-box clients can't connect
    finally:
        srv.shutdown()
        srv.server_close()


def test_events_endpoint_reflects_the_bus():
    bus = EventBus()
    bus.emit("query.received", asker="peerX", query="hi")
    srv, token = serve_control(node_id="n", events=bus)
    try:
        status, body = _get(srv.server_address, "/events", token)
        evs = json.loads(body)
        assert status == 200 and len(evs) == 1 and evs[0]["type"] == "query.received"
        assert evs[0]["detail"]["asker"] == "peerX"
    finally:
        srv.shutdown()
        srv.server_close()


def test_storage_endpoint():
    cache = ArtifactCache(quota_bytes=1234)
    srv, token = serve_control(node_id="n", cache=cache)
    try:
        status, body = _get(srv.server_address, "/storage", token)
        d = json.loads(body)
        assert status == 200 and d["quota_bytes"] == 1234 and d["count"] == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_served_without_token_but_data_still_gated():
    srv, _token = serve_control(node_id="n", events=EventBus())
    try:
        addr = srv.server_address
        status, body = _get(addr, "/")                     # static page, no token needed
        assert status == 200 and b"<!doctype html>" in body.lower() and b"AIMessage node" in body
        assert _get(addr, "/status")[0] == 401             # ...but data endpoints still require the token
    finally:
        srv.shutdown()
        srv.server_close()


def _post(addr, path, obj, token=None):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", path, body=json.dumps(obj).encode(), headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


class _FakeMem:
    def __init__(self, content, type_="semantic", origin="orig", trust=0.9):
        self.content, self.type, self.origin_node, self.trust_score = content, type_, origin, trust


def test_ask_requires_token_and_returns_answers():
    calls = []
    def fake_ask(text, window):
        calls.append((text, window))
        return [_FakeMem("biologics qualify for CCM")]
    clock = {"t": 0.0}
    srv, token = serve_control(node_id="n", ask=fake_ask, clock=lambda: clock["t"])
    try:
        addr = srv.server_address
        assert _post(addr, "/ask", {"query": "ccm"})[0] == 401           # no token
        status, body = _post(addr, "/ask", {"query": "ccm"}, token)
        d = json.loads(body)
        assert status == 200 and d["answers"][0]["content"] == "biologics qualify for CCM"
        assert calls == [("ccm", 4.0)]
    finally:
        srv.shutdown()
        srv.server_close()


def test_ask_writeback_hook_reports_count():
    # Phase 7: /ask absorbs answers into the local hub and reports how many via `wrote_back`.
    absorbed = []
    def fake_ask(text, window):
        return [_FakeMem("biologics qualify for CCM"), _FakeMem("chronic care duration")]
    def fake_writeback(answers):
        absorbed.extend(answers)
        return len(answers)
    srv, token = serve_control(node_id="n", ask=fake_ask, writeback=fake_writeback,
                               clock=lambda: 0.0)
    try:
        status, body = _post(srv.server_address, "/ask", {"query": "ccm"}, token)
        d = json.loads(body)
        assert status == 200 and d["wrote_back"] == 2 and len(absorbed) == 2
    finally:
        srv.shutdown()
        srv.server_close()


def test_ask_writeback_failure_never_breaks_the_ask():
    # A hub hiccup on write-back must not fail the /ask (answers still returned, wrote_back=0).
    def boom(answers):
        raise OSError("hub down")
    srv, token = serve_control(node_id="n", ask=lambda t, w: [_FakeMem("x")],
                               writeback=boom, clock=lambda: 0.0)
    try:
        status, body = _post(srv.server_address, "/ask", {"query": "q"}, token)
        d = json.loads(body)
        assert status == 200 and d["answers"] and d["wrote_back"] == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_ask_is_rate_limited():
    clock = {"t": 100.0}
    srv, token = serve_control(node_id="n", ask=lambda t, w: [], ask_min_interval=1.0,
                               clock=lambda: clock["t"])
    try:
        addr = srv.server_address
        assert _post(addr, "/ask", {"query": "a"}, token)[0] == 200
        assert _post(addr, "/ask", {"query": "a"}, token)[0] == 429     # within window
        clock["t"] = 102.0
        assert _post(addr, "/ask", {"query": "a"}, token)[0] == 200     # window elapsed
    finally:
        srv.shutdown()
        srv.server_close()


def test_ask_validates_body_and_availability():
    srv, token = serve_control(node_id="n", ask=lambda t, w: [])
    try:
        addr = srv.server_address
        assert _post(addr, "/ask", {"nope": 1}, token)[0] == 400        # missing query
        assert _post(addr, "/ask", {"query": "   "}, token)[0] == 400   # blank query
    finally:
        srv.shutdown()
        srv.server_close()
    srv2, token2 = serve_control(node_id="n")                            # ask not provided
    try:
        assert _post(srv2.server_address, "/ask", {"query": "x"}, token2)[0] == 501
    finally:
        srv2.shutdown()
        srv2.server_close()


def test_memories_endpoint():
    srv, token = serve_control(node_id="n", memories=lambda: [_FakeMem("my shared fact")])
    try:
        status, body = _get(srv.server_address, "/memories", token)
        d = json.loads(body)
        assert status == 200 and d[0]["content"] == "my shared fact"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_path_404_with_token():
    srv, token = serve_control(node_id="n")
    try:
        assert _get(srv.server_address, "/nope", token)[0] == 404
        assert _get(srv.server_address, "/nope")[0] == 401     # token still checked first
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
