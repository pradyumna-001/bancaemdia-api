"""Configured Ed25519 signing; private key never enters a publication or log."""

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from bancaemdia.coleta.catalogo import StrictModel, canonical


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class TrustRoot(StrictModel):
    key_id: str
    algorithm: str = "Ed25519"
    public_key: str


class CatalogSigner:
    def __init__(
        self, key_id: str, key: Ed25519PrivateKey, next_root: TrustRoot | None = None
    ) -> None:
        if not key_id or len(key_id) > 80:
            raise ValueError("invalid key ID")
        self.key_id = key_id
        self.key = key
        self.current = TrustRoot(key_id=key_id, public_key=b64(key.public_key().public_bytes_raw()))
        if next_root is not None:
            if next_root.key_id == key_id or next_root.algorithm != "Ed25519":
                raise ValueError("next key must have a distinct ID and use Ed25519")
            Ed25519PublicKey.from_public_bytes(
                base64.urlsafe_b64decode(next_root.public_key + "==")
            )
        self.next = next_root

    @classmethod
    def from_file(
        cls, key_id: str, path: Path, next_root: TrustRoot | None = None
    ) -> "CatalogSigner":
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("catalog requires an Ed25519 private key")
        return cls(key_id, key, next_root)

    def sign(self, payload: dict[str, object]) -> dict[str, object]:
        document = dict(payload)
        document["key_id"] = self.key_id
        document["algorithm"] = "Ed25519"
        document["trust_roots"] = {
            "current": self.current.model_dump(),
            "next": self.next.model_dump() if self.next else None,
        }
        return {"payload": document, "signature": b64(self.key.sign(canonical(document)))}
