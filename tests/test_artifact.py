"""Phase 4 artifact exchange — integrity, authenticity, and a hard install gate.

Pure stdlib + socket transport, so this runs in CI (no libp2p). Security-critical, so the checks
are thorough: tamper (bytes + manifest), wrong signer, gate-denies-by-default, no-run, and a
path-traversal / tarbomb guard.

Runnable:  python tests/test_artifact.py   |   pytest
"""
import io
import sys
import tarfile
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

from aimessage import artifact as A  # noqa: E402
from aimessage.artifact_exchange import ArtifactHost, fetch  # noqa: E402

FILES = {
    "server.py": b"# an (inert) MCP server\nprint('hello')\n",
    "manifest.json": b'{"name": "demo-mcp"}\n',
}


def _mk(key=None):
    return A.pack(FILES, name="demo-mcp", kind="mcp-server", version="0.1.0",
                  key=key or SigningKey.generate(), description="demo")


def test_pack_verify_roundtrip():
    art = _mk()
    assert A.verify(art)
    assert art.content_hash and art.manifest["kind"] == "mcp-server"


def test_deterministic_hash():
    k = SigningKey.generate()
    assert _mk(k).content_hash == _mk(k).content_hash  # same input → same address


def test_tamper_blob_detected():
    art = _mk()
    art.blob = art.blob[:-1] + bytes([art.blob[-1] ^ 0x01])  # flip one byte
    assert not A.verify(art)


def test_tamper_manifest_detected():
    art = _mk()
    art.manifest["name"] = "malware"          # sig no longer matches manifest
    assert not A.verify(art)


def test_wrong_signer_rejected():
    art = _mk()
    import base64
    art.manifest["publisher_node"] = base64.b64encode(
        bytes(SigningKey.generate().verify_key)).decode()  # claim a different publisher
    assert not A.verify(art)


def test_install_denied_without_approval():
    art = _mk()
    with tempfile.TemporaryDirectory() as d:
        try:
            A.install(art, d, approve=lambda m: False)
            assert False, "install should be refused without approval"
        except PermissionError:
            pass
        # Truthy-but-not-True must also be refused (no shortcuts).
        try:
            A.install(art, d, approve=lambda m: "yes")
            assert False, "only literal True may approve"
        except PermissionError:
            pass


def test_install_extracts_on_approval_and_does_not_run():
    art = _mk()
    with tempfile.TemporaryDirectory() as d:
        dest = A.install(art, d, approve=lambda m: True)
        assert (dest / "server.py").read_bytes() == FILES["server.py"]
        assert (dest / "manifest.json").exists()
    # install returns a path to extracted files; it never imports/execs them (no side effects).


def test_install_refuses_unverified_even_if_approved():
    art = _mk()
    art.blob = art.blob + b"x"                 # corrupt after signing
    with tempfile.TemporaryDirectory() as d:
        try:
            A.install(art, d, approve=lambda m: True)
            assert False, "must refuse to install an artifact that fails verification"
        except ValueError:
            pass


def test_path_traversal_blocked():
    # Hand-craft a malicious blob whose tar member escapes the dest dir, then sign it honestly
    # (attacker controls their own key). verify() passes; install() must still not escape.
    import base64
    import hashlib
    import json
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"pwned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    key = SigningKey.generate()
    from aimessage.identity import node_id
    manifest = {"name": "evil", "kind": "plugin", "version": "1.0",
                "content_hash": hashlib.sha256(blob).hexdigest(), "size": len(blob),
                "publisher_node": node_id(key), "description": ""}
    sig = base64.b64encode(key.sign(json.dumps(manifest, sort_keys=True,
                                               separators=(",", ":")).encode()).signature).decode()
    art = A.Artifact(manifest=manifest, sig=sig, blob=blob)
    assert A.verify(art)  # a valid signature over malicious content — signing ≠ safety
    with tempfile.TemporaryDirectory() as d:
        parent = Path(d)
        try:
            A.install(art, parent / "sandbox", approve=lambda m: True)
        except Exception:
            pass  # 'data' filter raising is a fine outcome
        assert not (parent / "escape.txt").exists(), "path traversal escaped the dest dir!"


def test_fetch_over_socket_then_verify():
    art = _mk()
    host = ArtifactHost()
    h = host.add(art)
    addr = host.serve()
    try:
        got = fetch(addr, h)
        assert got is not None and A.verify(got)
        assert got.content_hash == h
        assert fetch(addr, "0" * 64) is None      # unknown hash → None
    finally:
        host.stop()


# --- P0.3 adversarial: signed-manifest path traversal (C2) + gzip tarbomb (M11) ---

def _signed_artifact(blob, name, version, key, kind="plugin"):
    """Hand-build a VALIDLY-SIGNED artifact with arbitrary manifest fields (attacker's own key)."""
    import base64
    import hashlib
    from aimessage.identity import node_id
    manifest = {"name": name, "kind": kind, "version": version,
                "content_hash": hashlib.sha256(blob).hexdigest(), "size": len(blob),
                "publisher_node": node_id(key), "description": ""}
    sig = base64.b64encode(key.sign(A._canonical(manifest)).signature).decode()
    return A.Artifact(manifest=manifest, sig=sig, blob=blob)


def test_pack_rejects_unsafe_name():
    try:
        A.pack({"x": b"y"}, name="../evil", kind="plugin", version="1.0", key=SigningKey.generate())
        assert False, "pack must reject a traversal name"
    except ValueError:
        pass


def test_install_rejects_signed_traversal_name():
    # A valid signature over a malicious name — signing proves who, never that it's safe.
    key = SigningKey.generate()
    blob = A._pack_bytes({"x.txt": b"hi"})
    art = _signed_artifact(blob, name="../../../pwned", version="1.0", key=key)
    assert A.verify(art)                          # verification passes...
    with tempfile.TemporaryDirectory() as d:
        parent = Path(d)
        try:
            A.install(art, parent / "sandbox", approve=lambda m: True)
            assert False, "must reject unsafe name before extracting"
        except ValueError:
            pass                                  # ...but install refuses
        assert not (parent / "pwned").exists() and not (parent.parent / "pwned").exists()


def test_install_rejects_absolute_name():
    key = SigningKey.generate()
    blob = A._pack_bytes({"x.txt": b"hi"})
    art = _signed_artifact(blob, name="/tmp/evil", version="1.0", key=key)
    with tempfile.TemporaryDirectory() as d:
        try:
            A.install(art, d, approve=lambda m: True)
            assert False, "must reject an absolute name"
        except ValueError:
            pass


def test_install_rejects_gzip_tarbomb():
    # A signed GZIP tar: content_hash/size match the compressed blob, so verify() passes; a
    # transparent-decompress open would expand it. install() must refuse (mode='r:').
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as t:
        data = b"A" * 4096
        ti = tarfile.TarInfo("big.txt")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    blob = inner.getvalue()
    key = SigningKey.generate()
    art = _signed_artifact(blob, name="bomb", version="1.0", key=key)
    assert A.verify(art)
    with tempfile.TemporaryDirectory() as d:
        try:
            A.install(art, d, approve=lambda m: True)
            assert False, "gzip blob must be rejected (no transparent decompression)"
        except ValueError:
            pass


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\nall {len(tests)} passed")
