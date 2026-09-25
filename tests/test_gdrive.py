"""Google Drive cold-tier backend + both-full degradation. Uses a FAKE DriveClient → CI-safe
(no google-api-python-client, no OAuth). The real GoogleDriveClient wiring is exercised manually.

Runnable:  python tests/test_gdrive.py   |   pytest
"""
from nacl.signing import SigningKey

from aimessage import artifact as A
from aimessage.events import STORAGE_FULL, EventBus
from aimessage.gdrive import GDriveColdStore
from aimessage.storage import TieredArtifactCache

_K = SigningKey.generate()


def _art(name, filler=1000):
    return A.pack({"f.txt": b"x" * filler, "n": name.encode()},
                  name=name, kind="plugin", version="1.0", key=_K)


class FakeDriveClient:
    """In-memory stand-in for the Drive v3 client (upload/download/exists/free_bytes)."""
    def __init__(self, free=None):
        self.files: dict[str, bytes] = {}
        self._free = free

    def upload(self, name, data):
        if self._free is not None and self._free < len(data):
            raise RuntimeError("drive full")
        self.files[name] = data

    def download(self, name):
        return self.files.get(name)

    def exists(self, name):
        return name in self.files

    def free_bytes(self):
        return self._free


def test_gdrive_cold_store_put_get_contains():
    cold = GDriveColdStore(FakeDriveClient())
    h = "a" * 64
    cold.put(h, b"ciphertext")
    assert h in cold and cold.get(h) == b"ciphertext"
    assert ("b" * 64) not in cold and cold.get("b" * 64) is None


def test_gdrive_cold_store_rejects_non_hex_key():
    cold = GDriveColdStore(FakeDriveClient())
    for bad in ("../escape", "not-a-hash"):
        try:
            cold.put(bad, b"x")
            assert False, f"must reject {bad!r}"
        except ValueError:
            pass
    assert cold.get("../escape") is None            # get is lenient → None, never traverses


def test_tiered_cache_over_gdrive_spills_and_recovers():
    a, b = _art("a"), _art("b")
    cold = GDriveColdStore(FakeDriveClient())       # unlimited
    cache = TieredArtifactCache(_K, cold, quota_bytes=len(a.blob))
    cache.add(a)
    cache.add(b)                                    # evicts a → spills (encrypted) to fake Drive
    assert a.content_hash in cold
    got = cache.get(a.content_hash)                 # transparent pull-back + decrypt + verify
    assert got is not None and A.verify(got)


def test_both_full_emits_storage_full_and_keeps_serving():
    a, b = _art("a"), _art("b")
    cold = GDriveColdStore(FakeDriveClient(free=0))  # Drive reports no space
    bus = EventBus()
    cache = TieredArtifactCache(_K, cold, quota_bytes=len(a.blob), events=bus)
    cache.add(a)
    cache.add(b)                                    # evicts a; cold is full → drop a, emit storage.full
    full = [e for e in bus.recent() if e.type == STORAGE_FULL]
    assert len(full) == 1 and full[0].detail["backend"] == "GDriveColdStore"
    assert cache.get(b.content_hash) is not None    # hot still serves
    assert cache.get(a.content_hash) is None        # a was dropped (re-fetchable by hash elsewhere)


def test_storage_full_notice_is_throttled():
    a, b, c = _art("a"), _art("b"), _art("c")
    clock = {"t": 0.0}
    cold = GDriveColdStore(FakeDriveClient(free=0))
    bus = EventBus()
    cache = TieredArtifactCache(_K, cold, quota_bytes=len(a.blob), events=bus,
                                full_notice_window_s=300.0, clock=lambda: clock["t"])
    cache.add(a)
    cache.add(b)          # evict a → storage.full (1st)
    cache.add(c)          # evict b → within window → throttled, no 2nd notice
    assert len([e for e in bus.recent() if e.type == STORAGE_FULL]) == 1
    clock["t"] = 400.0
    cache.add(_art("d"))  # evict c → window elapsed → 2nd notice
    assert len([e for e in bus.recent() if e.type == STORAGE_FULL]) == 2


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
