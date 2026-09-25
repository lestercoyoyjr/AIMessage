"""Phase 1 checks: signing, tamper detection, owner gating, content addressing.

Runnable two ways:  python tests/test_memory.py   |   pytest
"""
import base64
import sys
from pathlib import Path

from nacl.signing import SigningKey

from aimessage.identity import load_or_create, node_id  # noqa: E402
from aimessage.memory import Owner, PortableMemory  # noqa: E402


def _mem(**kw):
    base = dict(
        content="biologic patients on 3-month intervals qualify for CCM",
        type="semantic",
        owner=Owner.FEDERATED.value,
        trust_score=0.8,
    )
    base.update(kw)
    return PortableMemory(**base)


def test_sign_verify_roundtrip():
    m = _mem().sign(SigningKey.generate())
    assert m.verify()
    assert m.origin_node and m.sig and m.created > 0


def test_tamper_detected():
    m = _mem().sign(SigningKey.generate())
    m.content = "tampered payload"
    assert not m.verify()


def test_wrong_key_rejected():
    k1, k2 = SigningKey.generate(), SigningKey.generate()
    m = _mem().sign(k1)
    # Claim a different origin than the one that actually signed → must fail.
    m.origin_node = base64.b64encode(bytes(k2.verify_key)).decode()
    assert not m.verify()


def test_unsigned_does_not_verify():
    assert not _mem().verify()


def test_owner_gating():
    assert _mem(owner=Owner.FEDERATED.value).is_shareable
    assert not _mem(owner=Owner.SHARED.value).is_shareable
    assert not _mem(owner=Owner.PERSONAL.value).is_shareable


def test_json_roundtrip_preserves_signature_and_id():
    m = _mem().sign(SigningKey.generate())
    m2 = PortableMemory.from_json(m.to_json())
    assert m2.verify()
    assert m2.id == m.id


def test_content_address_stable_across_nodes():
    # Same signed payload hashes identically regardless of who holds it.
    m = _mem(created=123.0).sign(SigningKey.generate())
    assert m.id == PortableMemory.from_json(m.to_json()).id


def test_identity_is_stable_and_private(tmp_path=None):
    import os
    import stat
    p = Path(tmp_path) if tmp_path else Path("/tmp/aimsg_test_identity.key")
    if p.exists():
        p.unlink()
    k1 = load_or_create(p)
    k2 = load_or_create(p)  # second call must load, not regenerate
    assert node_id(k1) == node_id(k2)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600  # private key locked down (created via O_EXCL)
    p.unlink()


def test_identity_rejects_corrupt_key():
    p = Path("/tmp/aimsg_test_corrupt.key")
    p.write_text("this is not a valid ed25519 seed")   # wrong length / garbage
    try:
        load_or_create(p)
        assert False, "a corrupt key file must raise a clear error, not a raw crash"
    except ValueError:
        pass
    finally:
        p.unlink()


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
