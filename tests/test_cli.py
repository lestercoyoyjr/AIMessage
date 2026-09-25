"""CLI wiring — arg parsing + the memories-file store loader. Pure (no libp2p) → runs in CI.

Runnable:  python tests/test_cli.py   |   pytest
"""
import json
import sys
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import cli  # noqa: E402
from aimessage.memory import Owner  # noqa: E402


def test_store_from_file_signs_and_defaults_to_federated():
    rows = [
        {"content": "patients on a biologic qualify for chronic care management", "type": "semantic"},
        {"content": "a personal note", "owner": "personal"},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(rows, f)
        path = f.name
    try:
        store = cli._store_from_file(path, SigningKey.generate())
    finally:
        Path(path).unlink()
    assert len(store.memories) == 2
    assert all(m.verify() for m in store.memories)          # all signed by the node key
    assert store.memories[0].owner == Owner.FEDERATED.value  # default tier
    assert store.memories[1].owner == Owner.PERSONAL.value   # explicit override respected
    # search only returns federated → the personal note is never served
    hits = store.search("chronic care management biologic")
    assert len(hits) == 1 and hits[0].owner == Owner.FEDERATED.value


def test_parser_serve_ask_id():
    p = cli.build_parser()
    a = p.parse_args(["serve", "--listen", "/ip4/0.0.0.0/tcp/4001", "--memories", "m.json"])
    assert a.cmd == "serve" and a.memories == "m.json" and a.func is cli.cmd_serve
    b = p.parse_args(["ask", "hello there", "--bootstrap", "/ip4/1.2.3.4/tcp/4001/p2p/Qm"])
    assert b.cmd == "ask" and b.query == "hello there" and b.func is cli.cmd_ask
    c = p.parse_args(["id"])
    assert c.cmd == "id" and c.func is cli.cmd_id


def test_serve_centralaizer_and_memories_mutually_exclusive():
    p = cli.build_parser()
    try:
        p.parse_args(["serve", "--centralaizer", "http://x", "--memories", "m.json"])
        assert False, "should reject both sources"
    except SystemExit:
        pass


def test_sanitize_strips_controls_and_newlines():
    # NEW-5: ESC/BEL/C1 stripped, and newlines neutralized so peer content can't forge a status line.
    assert cli._sanitize("ok\x1b[2Jx\x07") == "ok [2Jx "        # ESC + BEL → space (escape defused)
    assert "\n" not in cli._sanitize("real\nfake status line")   # newline neutralized
    assert cli._sanitize("c1\x9bcsi") == "c1 csi"                # C1 (0x9B) stripped
    assert cli._sanitize("plain text") == "plain text"


def test_ask_requires_bootstrap():
    p = cli.build_parser()
    try:
        p.parse_args(["ask", "hello"])   # no --bootstrap
        assert False, "ask must require --bootstrap"
    except SystemExit:
        pass


def test_read_dir_collects_files():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.py").write_bytes(b"print(1)")
        (Path(d) / "sub").mkdir()
        (Path(d) / "sub" / "b.txt").write_bytes(b"hi")
        files = cli._read_dir(d)
        assert files == {"a.py": b"print(1)", "sub/b.txt": b"hi"}


def test_artifact_parsers():
    p = cli.build_parser()
    a = p.parse_args(["artifact-serve", "./bundle", "--name", "w", "--cold-dir", "/cold"])
    assert a.cmd == "artifact-serve" and a.name == "w" and a.cold_dir == "/cold" and a.func is cli.cmd_artifact_serve
    b = p.parse_args(["artifact-fetch", "abc", "--addr", "127.0.0.1:9", "--install", "/dst"])
    assert b.cmd == "artifact-fetch" and b.addr == "127.0.0.1:9" and b.func is cli.cmd_artifact_fetch


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
