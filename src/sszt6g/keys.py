"""Ed25519 identities.

Fixture identities are derived deterministically from a public label so that bundled fixtures are
reproducible. THEY ARE NOT SECRETS and must never protect anything real.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


@dataclass(frozen=True)
class Identity:
    name: str
    private: Ed25519PrivateKey = field(repr=False, compare=False)
    public_hex: str

    def sign(self, message: bytes) -> str:
        return self.private.sign(message).hex()


def fixture_identity(name: str) -> Identity:
    seed = hashlib.sha256(("sszt6g-demo-fixture-key:" + name).encode("utf-8")).digest()
    private = Ed25519PrivateKey.from_private_bytes(seed)
    pub = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return Identity(name=name, private=private, public_hex=pub.hex())


def verify_signature(public_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
