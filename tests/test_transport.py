"""P0.4 resource bounds: frame-size cap (M1) + stalled-read handling (M2). Pure stdlib → CI.

Runnable:  python tests/test_transport.py   |   pytest
"""
import socket
import struct
import sys
from pathlib import Path

from aimessage import transport as T  # noqa: E402


class _FakeSock:
    """Feeds bytes to _recvn/_read_frame; tracks how much was consumed."""
    def __init__(self, data: bytes):
        self.buf = bytearray(data)

    def recv(self, n: int) -> bytes:
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk

    def settimeout(self, _t):        # deadline path calls this
        pass


class _TimeoutSock:
    def recv(self, n):
        raise socket.timeout()


def test_read_frame_refuses_oversized_without_reading_body():
    # Header declares 2 GiB; body follows. With a 1 KiB cap the frame is refused and the body
    # is NOT consumed (proves we never allocated / read the huge payload).
    body = b"x" * 10
    fs = _FakeSock(struct.pack("!I", 2 << 30) + body)
    assert T._read_frame(fs, max_bytes=1024) == b""
    assert bytes(fs.buf) == body            # body untouched — no giant read attempted


def test_read_frame_accepts_within_cap():
    fs = _FakeSock(struct.pack("!I", 5) + b"hello")
    assert T._read_frame(fs, max_bytes=1024) == b"hello"


def test_recvn_treats_timeout_as_eof():
    # A stalled peer must not hang the reader (M2): recv raising timeout → returns what it has.
    assert T._recvn(_TimeoutSock(), 100) == b""


def test_oversized_frame_over_real_socket_is_refused_and_server_survives():
    srv = T.run_server("127.0.0.1", 0, lambda raw: b"handled:" + raw, max_request=64)
    host, port = srv.server_address
    try:
        s = socket.create_connection((host, port), 2)
        s.sendall(struct.pack("!I", 10_000_000))   # declare ~10 MB, over the 64-byte cap
        s.sendall(b"a" * 100)
        s.settimeout(2)
        reply = T._read_frame(s)                    # server refused → closed w/o handling
        s.close()
        assert reply == b""
        # server still alive: a valid small request is still served
        assert T.send_request(host, port, b"ok") == b"handled:ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_valid_roundtrip_over_socket():
    srv = T.run_server("127.0.0.1", 0, lambda raw: b"echo:" + raw)
    try:
        assert T.send_request(*srv.server_address, b"hi") == b"echo:hi"
    finally:
        srv.shutdown()
        srv.server_close()


def test_read_frame_honors_deadline():
    # M2: a deadline already in the past → return b"" without reading the body (bounds slow-drip).
    fs = _FakeSock(struct.pack("!I", 5) + b"hello")
    assert T._read_frame(fs, deadline=T.time.monotonic() - 1) == b""
    assert len(fs.buf) == 9                       # nothing consumed — broke before recv


def test_connection_cap_drops_excess_connections():
    # M2: with max_conns=1, a second concurrent connection is dropped (bounds thread exhaustion).
    import threading
    acquired, release = threading.Event(), threading.Event()

    def handler(raw):
        acquired.set()          # signal that this handler holds the only slot
        release.wait(5)
        return b"ok"

    srv = T.run_server("127.0.0.1", 0, handler, max_conns=1)
    host, port = srv.server_address
    try:
        s1 = socket.create_connection((host, port), 2)
        T._write_frame(s1, b"hi")
        assert acquired.wait(5)                   # deterministic: slot now occupied by s1's handler
        s2 = socket.create_connection((host, port), 2)
        T._write_frame(s2, b"hi")
        s2.settimeout(3)
        assert T._read_frame(s2) == b""           # dropped (slot full) → connection closed, no reply
        release.set()
        s1.settimeout(3)
        assert T._read_frame(s1) == b"ok"         # s1 still served
        s1.close()
        s2.close()
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
