"""Content-addressed, signed artifacts — the safe way to share plugins & MCP servers.

Sharing a plugin or an MCP server means shipping CODE to another machine. That's arbitrary-code-
execution risk, so this module is deliberately strict:

  * **Content-addressed** — an artifact's id is sha256 of its bytes. Integrity is checked on arrival
    by recomputing the hash; a single flipped byte fails verification.
  * **Signed** — the publisher signs the manifest (which pins the content hash) with their Ed25519
    node key. Receivers verify the signature against the publisher's node id.
  * **Human-gated install** — `install()` refuses unless an `approve(manifest)` callback explicitly
    returns True. There is no "install if trusted" default.
  * **Never runs anything** — install only extracts files (with the stdlib 'data' filter to block
    path-traversal / tarbombs). Running the artifact is a separate, explicit human action afterward.

Pure stdlib + pynacl — no networking here, so it's fully testable in CI. Distribution lives in
artifact_exchange.py.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .identity import node_id

VALID_KINDS = ("plugin", "mcp-server")
MAX_EXTRACT_BYTES = 64 * 1024 * 1024   # per-artifact extracted-size ceiling (tarbomb guard); code
# bundles are small — aligned with artifact_exchange.MAX_ARTIFACT_FRAME so fetch and install agree.

# A safe single path component: letters/digits/dot/dash/underscore, not "." / ".." / leading-dot.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_component(value, field: str) -> str:
    """Reject anything that could escape the destination dir when used to build a path.

    `name`/`version` come from an attacker-SIGNED manifest — a valid signature proves *who* wrote
    them, never that they're a safe filename. `../..`, absolute paths, and separators are refused here.
    """
    if not isinstance(value, str) or value in (".", "..") or not _SAFE_COMPONENT.match(value):
        raise ValueError(f"unsafe manifest {field}: {value!r}")
    return value


def _pack_bytes(files: dict[str, bytes]) -> bytes:
    """Deterministic tar of {relpath: content} → same input always yields the same bytes/hash."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


@dataclass
class Artifact:
    manifest: dict   # signed: name, kind, version, content_hash, size, publisher_node, description
    sig: str         # b64 Ed25519 signature over the canonical manifest
    blob: bytes      # the tar bytes (transferred alongside; bound to manifest via content_hash)

    @property
    def content_hash(self) -> str:
        return self.manifest["content_hash"]


def pack(files: dict[str, bytes], *, name: str, kind: str, version: str,
         key: SigningKey, description: str = "") -> Artifact:
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    _safe_component(name, "name")        # reject unsafe names at creation, not just at install
    _safe_component(version, "version")
    blob = _pack_bytes(files)
    manifest = {
        "name": name,
        "kind": kind,
        "version": version,
        "content_hash": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "publisher_node": node_id(key),
        "description": description,
    }
    sig = base64.b64encode(key.sign(_canonical(manifest)).signature).decode()
    return Artifact(manifest=manifest, sig=sig, blob=blob)


def verify_manifest(manifest: dict, sig: str) -> bool:
    """True iff `sig` is a valid publisher signature over `manifest` — checkable WITHOUT the blob
    (catalog adverts carry manifest+sig only). Blob integrity is still enforced by `verify()` on fetch."""
    try:
        VerifyKey(base64.b64decode(manifest["publisher_node"])).verify(
            _canonical(manifest), base64.b64decode(sig))
        return True
    except (BadSignatureError, ValueError, KeyError, TypeError):
        return False


def verify(art: Artifact) -> bool:
    """True iff the blob matches the pinned hash/size AND the manifest signature is valid."""
    m = art.manifest
    try:
        if hashlib.sha256(art.blob).hexdigest() != m["content_hash"]:
            return False
        if len(art.blob) != m["size"]:
            return False
        if not verify_manifest(m, art.sig):
            return False
        return True
    except (BadSignatureError, ValueError, KeyError, TypeError):
        return False


def install(art: Artifact, dest_dir: str | Path, approve: Callable[[dict], bool]) -> Path:
    """Verify → require explicit human approval → extract (never execute).

    `approve(manifest)` MUST return exactly True or the install is refused. Extraction uses the
    stdlib 'data' filter, which rejects absolute paths, `..` traversal, and links (tarbomb guard).
    """
    if not verify(art):
        raise ValueError("artifact failed verification — refusing to install")

    # Sanitize BEFORE prompting: a validly-signed manifest can still carry a malicious name/version.
    name = _safe_component(art.manifest.get("name"), "name")
    version = _safe_component(art.manifest.get("version"), "version")

    if approve(art.manifest) is not True:      # hard gate: no truthy shortcuts, no default-yes
        raise PermissionError("install not approved by the human gate")

    base = Path(dest_dir).resolve()
    dest = base / f"{name}-{version}"
    if not dest.resolve().is_relative_to(base):   # belt-and-suspenders: never escape dest_dir
        raise ValueError("install path escaped the destination directory")
    dest.mkdir(parents=True, exist_ok=True)

    try:
        # mode="r:" = NO transparent decompression → a signed gzip tarbomb (small blob, huge
        # expansion) fails to open here instead of filling the disk. pack() writes uncompressed tar.
        with tarfile.open(fileobj=io.BytesIO(art.blob), mode="r:") as tar:
            total = sum(max(m.size, 0) for m in tar.getmembers())
            if total > MAX_EXTRACT_BYTES:
                raise ValueError(f"artifact extracts to {total} bytes, over {MAX_EXTRACT_BYTES}")
            tar.extractall(dest, filter="data")   # 'data' filter blocks traversal / links / abs paths
    except tarfile.TarError as e:
        raise ValueError(f"artifact is not a valid uncompressed tar: {e}") from e
    return dest
    # NOTE: we extract and stop. Importing/executing the artifact is the user's explicit, separate
    # step after they've reviewed the files. This module never runs shared code.
