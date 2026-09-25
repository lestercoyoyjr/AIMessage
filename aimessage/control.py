"""Local control API (Desktop v1.0) — a localhost HTTP surface a UI uses to observe AND drive a node.

New attack surface = locked down two ways: **localhost-bind** (127.0.0.1) + a **per-launch bearer
token** (constant-time compare; 401 before any work). Read GETs (`/status`, `/events`, `/storage`,
`/memories`) plus a state-changing `POST /ask` (broadcast a query to the mesh). `/ask` is rate-limited
and body-size-capped; a leaked token can drive at human speed at worst, never flood.

The `ask` callable is injected by the daemon (it bridges the HTTP thread to the node's trio loop), so
this module has no trio/libp2p dependency and stays fully unit-testable with a fake `ask`.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_MAX_BODY = 64 * 1024


def _serialize_mem(m) -> dict:
    return {"content": m.content, "type": m.type,
            "origin_node": getattr(m, "origin_node", ""), "trust_score": m.trust_score}


def serve_control(*, node_id: str, events=None, cache=None, ask=None, memories=None,
                  writeback=None, host: str = "127.0.0.1", port: int = 0, token: str | None = None,
                  ask_min_interval: float = 1.0, clock=time.monotonic):
    """Start the control API in a background thread. Returns (server, token).
    `ask(text, window) -> list` (optional) enables POST /ask; `memories() -> list` enables GET /memories.
    `writeback(answers) -> int` (optional) absorbs the /ask answers into the local hub (Phase 7) and
    the count is returned to the dashboard."""
    token = token or secrets.token_urlsafe(32)
    expected = f"Bearer {token}"
    ask_lock = threading.Lock()
    last_ask = [float("-inf")]        # first ask always passes; subsequent ones respect the interval

    def _status(_q):
        return {"node_id": node_id,
                "events_buffered": len(events.recent(n=10 ** 9)) if events else 0,
                "can_ask": ask is not None}

    def _events(q):
        if events is None:
            return []
        limit = int((q.get("limit") or ["50"])[0])
        type_ = (q.get("type") or [None])[0]
        return [{"type": e.type, "ts": e.ts, "detail": e.detail}
                for e in events.recent(n=limit, type_=type_)]

    def _storage(_q):
        if cache is None:
            return {}
        return {"total_bytes": cache.total_bytes, "quota_bytes": cache.quota_bytes,
                "count": len(cache)}

    def _memories(_q):
        return [_serialize_mem(m) for m in memories()] if memories else []

    get_routes = {"/status": _status, "/events": _events, "/storage": _storage, "/memories": _memories}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code: int, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("Authorization", ""), expected)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html", "/ui"):
                from .dashboard_html import DASHBOARD_HTML
                body = DASHBOARD_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            fn = get_routes.get(u.path)
            if fn is None:
                return self._send(404, {"error": "not found"})
            try:
                return self._send(200, fn(parse_qs(u.query)))
            except Exception:
                return self._send(500, {"error": "internal error"})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if urlparse(self.path).path != "/ask":
                return self._send(404, {"error": "not found"})
            if ask is None:
                return self._send(501, {"error": "ask not available on this node"})
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > _MAX_BODY:
                return self._send(413, {"error": "body too large"})
            try:                                             # validate BEFORE throttling — a bad
                body = json.loads(self.rfile.read(length) or b"{}")   # request shouldn't burn the slot
                query = body["query"]
                if not isinstance(query, str) or not query.strip():
                    raise ValueError
                window = min(max(float(body.get("window", 4.0)), 0.5), 30.0)
            except (ValueError, TypeError, KeyError):
                return self._send(400, {"error": "expected JSON {query: str, window?: number}"})
            with ask_lock:                                   # rate-limit only real, about-to-broadcast asks
                now = clock()
                if now - last_ask[0] < ask_min_interval:
                    return self._send(429, {"error": "rate limited"})
                last_ask[0] = now
            try:
                answers = ask(query, window)
            except Exception:
                return self._send(500, {"error": "ask failed"})
            wrote = 0
            if writeback is not None:
                try:
                    wrote = writeback(answers)           # Phase 7: absorb answers into the local hub
                except Exception:
                    wrote = 0                            # best-effort — never fail the ask on write-back
            return self._send(200, {"answers": [_serialize_mem(m) for m in answers], "wrote_back": wrote})

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, token
