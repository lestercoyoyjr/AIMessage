"""Minimal length-prefixed request/reply transport over TCP (stdlib only).

ponytail: this is the demo transport, not the real network. It exists to prove the
query/crypto primitive works between two independent processes over a socket. Swap
`send_request` / `run_server` for a libp2p node (gossipsub + Noise) in Phase 3-real /
Phase 6 — the protocol layer above it does not change.
"""
from __future__ import annotations

import socket
import socketserver
import struct
import threading
import time
from typing import Callable

# Default per-message ceiling (M1: a 4-byte length prefix otherwise permits a 4 GiB frame → OOM).
# 1 MiB is ample for queries/answers; the artifact path passes a larger explicit cap.
MAX_FRAME = 1 << 20
DEFAULT_MAX_CONNS = 128     # cap concurrent handler threads (bounds thread-exhaustion DoS)


def _recvn(sock: socket.socket, n: int, deadline: float | None = None) -> bytes:
    buf = bytearray()                                    # bytearray → no O(n^2) concat
    while len(buf) < n:
        if deadline is not None:                         # M2: enforce a TOTAL read deadline, so a
            remaining = deadline - time.monotonic()      # slow-drip peer can't reset a per-recv
            if remaining <= 0:                           # timeout forever
                break
            try:
                sock.settimeout(remaining)
            except OSError:
                break
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:                                  # timeout / reset / broken pipe → treat as EOF,
            break                                        # never let a hostile/broken peer raise here
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(sock: socket.socket, max_bytes: int = MAX_FRAME, deadline: float | None = None) -> bytes:
    hdr = _recvn(sock, 4, deadline)
    if len(hdr) < 4:
        return b""
    (n,) = struct.unpack("!I", hdr)
    if n > max_bytes:
        return b""                                       # refuse oversized frame WITHOUT allocating n
    return _recvn(sock, n, deadline)


def _write_frame(sock: socket.socket, data: bytes) -> None:
    sock.sendall(struct.pack("!I", len(data)) + data)


def send_request(host: str, port: int, data: bytes, timeout: float = 5.0,
                 max_reply: int = MAX_FRAME) -> bytes:
    with socket.create_connection((host, port), timeout) as s:
        s.settimeout(timeout)
        _write_frame(s, data)
        return _read_frame(s, max_reply, deadline=time.monotonic() + timeout)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_server(host: str, port: int, handler: Callable[[bytes], bytes],
               max_request: int = MAX_FRAME, timeout: float = 10.0,
               max_conns: int = DEFAULT_MAX_CONNS) -> _Server:
    """Start a threaded server in the background. Returns it; `.server_address` gives the bound
    (host, port) (use port=0 for an ephemeral one); call `.shutdown()` + `.server_close()` to stop.

    `max_request` caps an inbound frame (M1). `timeout` is both the per-recv timeout AND the TOTAL
    read deadline, so a slow-drip peer is bounded to `timeout` seconds (M2). `max_conns` caps
    concurrent handler threads via a semaphore — excess connections are dropped, bounding
    thread-exhaustion DoS."""
    slots = threading.Semaphore(max_conns)

    class H(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            if not slots.acquire(blocking=False):
                return                                   # at capacity → drop the connection
            try:
                self.request.settimeout(timeout)
                req = _read_frame(self.request, max_request, deadline=time.monotonic() + timeout)
                if req:
                    _write_frame(self.request, handler(req))
            finally:
                slots.release()

    srv = _Server((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
