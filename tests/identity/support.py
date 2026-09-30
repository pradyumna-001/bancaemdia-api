import base64
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bancaemdia.auth.identity_config import IdentitySettings


def key_settings(directory: Path, **overrides) -> IdentitySettings:
    signing = directory / "signing.json"
    encryption = directory / "encryption.json"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    signing.write_text(json.dumps({"active": "test-rsa", "keys": {"test-rsa": pem}}))
    encryption.write_text(
        json.dumps({
            "active": "test-aes",
            "keys": {"test-aes": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()},
        })
    )
    return IdentitySettings(
        AUTH_SIGNING_KEYS_FILE=signing, AUTH_ENCRYPTION_KEYS_FILE=encryption, **overrides
    )
