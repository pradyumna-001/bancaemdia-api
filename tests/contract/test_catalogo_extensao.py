import base64
import copy

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from bancaemdia.coleta.catalogo import canonical
from bancaemdia.services.catalogo_assinatura import CatalogSigner, TrustRoot, b64


def verify(envelope, trusted, environment, known_version):
    payload = envelope["payload"]
    key = trusted[payload["key_id"]]
    key.verify(base64.urlsafe_b64decode(envelope["signature"] + "=="), canonical(payload))
    assert payload["environment"] == environment
    assert payload["catalog_version"] >= known_version


def test_signature_covers_exact_hosts_environment_version_and_rotation():
    key, next_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    next_root = TrustRoot(key_id="next", public_key=b64(next_key.public_key().public_bytes_raw()))
    signer = CatalogSigner("current", key, next_root)
    payload = {
        "environment": "testing",
        "catalog_version": 3,
        "entries": [{"hostname": "exact.bet.br", "rollout": "revoked"}],
    }
    signed = signer.sign(payload)
    trusted = {"current": key.public_key()}
    verify(signed, trusted, "testing", 2)
    for field, replacement in [
        ("environment", "production"),
        ("catalog_version", 1),
        ("entries", [{"hostname": "*.bet.br"}]),
        ("trust_roots", {}),
    ]:
        corrupted = copy.deepcopy(signed)
        corrupted["payload"][field] = replacement
        with pytest.raises(InvalidSignature):
            verify(corrupted, trusted, "testing", 2)
    with pytest.raises(AssertionError):
        verify(signed, trusted, "production", 2)
    with pytest.raises(AssertionError):
        verify(signed, trusted, "testing", 4)
    next_public = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(signed["payload"]["trust_roots"]["next"]["public_key"] + "==")
    )
    rotated = CatalogSigner("next", next_key).sign({**payload, "catalog_version": 4})
    verify(rotated, {"next": next_public}, "testing", 3)
    with pytest.raises(KeyError):
        verify(rotated, trusted, "testing", 3)
    assert "PRIVATE" not in str(signed)


def test_rotation_cannot_reuse_id_or_change_algorithm():
    key = Ed25519PrivateKey.generate()
    for root in (
        TrustRoot(key_id="current", public_key=b64(key.public_key().public_bytes_raw())),
        TrustRoot(key_id="next", algorithm="HS256", public_key="invalid"),
    ):
        with pytest.raises(ValueError):
            CatalogSigner("current", key, root)
