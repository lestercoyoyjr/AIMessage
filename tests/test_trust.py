"""Trust anchor — persisted, validated allowlist of trusted origin node-ids. Pure → CI.

Runnable:  python tests/test_trust.py   |   pytest
"""
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import cli
from aimessage.identity import node_id
from aimessage.trust import TrustStore, is_valid_node_id


def _nid():
    return node_id(SigningKey.generate())


def test_valid_node_id():
    assert is_valid_node_id(_nid())
    assert not is_valid_node_id("not-base64!!")
    assert not is_valid_node_id("YWJj")          # valid b64 but not 32 bytes
    assert not is_valid_node_id(123)


def test_add_persists_and_reloads():
    a, b = _nid(), _nid()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "trusted.json"
        s1 = TrustStore(p)
        s1.add(a)
        s1.add(b)
        assert a in s1 and len(s1) == 2
        s2 = TrustStore(p)                        # reload from disk
        assert a in s2 and b in s2 and s2.all() == {a, b}


def test_remove_and_reject_invalid():
    with tempfile.TemporaryDirectory() as d:
        s = TrustStore(Path(d) / "t.json")
        nid = _nid()
        s.add(nid)
        s.remove(nid)
        assert nid not in s and len(s) == 0
        try:
            s.add("garbage")
            assert False, "must reject an invalid node id"
        except ValueError:
            pass


def test_corrupt_file_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.json"
        p.write_text("not json {")
        s = TrustStore(p)                          # must not crash
        assert len(s) == 0


def test_ranking_uses_trusted_store_selection():
    # End-to-end: memories from a store-trusted origin outrank higher raw corroboration.
    from aimessage.memory import Owner, PortableMemory
    from aimessage.protocol import rank_corroborated
    kt = SigningKey.generate()
    with tempfile.TemporaryDirectory() as d:
        store = TrustStore(Path(d) / "t.json")
        store.add(node_id(kt))
        trusted = store.all()
    mt = PortableMemory(content="trusted", type="semantic", owner=Owner.FEDERATED.value,
                        trust_score=0.5).sign(kt)
    others = [("x", PortableMemory(content="untrusted", type="semantic",
                                   owner=Owner.FEDERATED.value, trust_score=0.9).sign(SigningKey.generate())),
              ("y", PortableMemory(content="untrusted", type="semantic",
                                   owner=Owner.FEDERATED.value, trust_score=0.9).sign(SigningKey.generate()))]
    ranked = rank_corroborated([("r", mt)] + others, trusted_origins=trusted)
    assert ranked[0].content == "trusted"


def test_trust_cli_parsers():
    p = cli.build_parser()
    a = p.parse_args(["trust", "add", "SOMEID", "--trust-file", "/tmp/t.json"])
    assert a.cmd == "trust" and a.trust_cmd == "add" and a.node_id == "SOMEID" and a.func is cli.cmd_trust
    b = p.parse_args(["trust", "list"])
    assert b.trust_cmd == "list" and b.node_id is None


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
