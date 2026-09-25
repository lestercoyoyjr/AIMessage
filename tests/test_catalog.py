"""Artifact catalog — manifest-only verification (pure) + gossip advertise/discover (self-skips).

Runnable:  python tests/test_catalog.py   |   pytest
"""
from nacl.signing import SigningKey

from aimessage import artifact as A


def _art(name="demo", key=None):
    return A.pack({"f.py": b"print(1)"}, name=name, kind="plugin", version="1.0",
                  key=key or SigningKey.generate())


def test_verify_manifest_without_blob():
    art = _art()
    assert A.verify_manifest(art.manifest, art.sig)          # valid publisher signature
    bad = dict(art.manifest, name="malware")                 # tampering the manifest
    assert not A.verify_manifest(bad, art.sig)
    assert not A.verify_manifest(art.manifest, "AAAA")       # bad sig
    assert not A.verify_manifest({"nope": 1}, art.sig)       # missing fields → False, not crash


# --- gossip catalog (self-skips without libp2p) ---
try:
    import trio

    from aimessage.gossip import GossipNode
    _HAVE_LIBP2P = True
except ImportError:
    _HAVE_LIBP2P = False


def test_advertise_and_discover_catalog():
    if not _HAVE_LIBP2P:
        print("  (skipped: libp2p not installed)")
        return
    pub = SigningKey.generate()
    art = _art("weather-mcp", pub)
    advertiser = GossipNode(lan_mode=True)
    listener = GossipNode(lan_mode=True)
    out = {}

    async def scenario():
        with trio.move_on_after(30):
            async with advertiser.running() as adv, listener.running() as lis:
                await adv.connect(lis.addr())
                await trio.sleep(2.0)
                # advertise a real artifact + a POISONED one (manifest tampered → sig invalid)
                await adv.advertise_artifact(art.manifest, art.sig, addr="127.0.0.1:9999")
                await adv.advertise_artifact(dict(art.manifest, name="evil"), art.sig, addr="x")
                await trio.sleep(2.0)
                out["catalog"] = lis.known_artifacts()

    trio.run(scenario)
    cat = out.get("catalog", [])
    assert len(cat) == 1                                     # poisoned advert rejected; only the real one
    assert cat[0]["manifest"]["content_hash"] == art.content_hash
    assert cat[0]["addr"] == "127.0.0.1:9999"


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
