"""Node identity: one Ed25519 keypair per hub = the node's "phone number".

The public key (base64) is the node id used as `origin_node` on every signed memory.
The private key is written 0600 and never leaves the machine.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from nacl.signing import SigningKey

# Lives beside Centralaizer's data so one machine = one identity.
DEFAULT_KEY_PATH = Path.home() / ".localmem" / "node_identity.key"


def load_or_create(path: Path | str = DEFAULT_KEY_PATH) -> SigningKey:
    """Return this node's signing key, generating + persisting one on first run."""
    path = Path(path)
    if path.exists():
        try:
            seed = base64.b64decode(path.read_text().strip())
        except Exception as e:
            raise ValueError(f"identity key at {path} is corrupt: {e}") from e
        if len(seed) != 32:
            raise ValueError(f"identity key at {path} is not a 32-byte Ed25519 seed")
        return SigningKey(seed)
    key = SigningKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create 0600 ATOMICALLY (O_EXCL) — the private key is never world-readable, even briefly,
    # closing the chmod-after-write race.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(bytes(key)).decode())
    return key


def node_id(key: SigningKey) -> str:
    """Public node id: base64 Ed25519 verify key."""
    return base64.b64encode(bytes(key.verify_key)).decode()
