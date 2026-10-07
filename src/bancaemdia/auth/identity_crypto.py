"""Key files stay on the server; ciphertext is bound to its row and purpose."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bancaemdia.auth.identity_config import IdentitySettings
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class IdentityKeys:
    def __init__(self, settings: IdentitySettings) -> None:
        if settings.AUTH_SIGNING_KEYS_FILE is None or settings.AUTH_ENCRYPTION_KEYS_FILE is None:
            raise ValueError("Identity key files are required")
        signing = self._read(settings.AUTH_SIGNING_KEYS_FILE)
        encryption = self._read(settings.AUTH_ENCRYPTION_KEYS_FILE)
        self.kid = str(signing["active"])
        self.enc_kid = str(encryption["active"])
        private = serialization.load_pem_private_key(signing["keys"][self.kid].encode(), None)
        if not isinstance(private, RSAPrivateKey) or private.key_size < 2048:
            raise ValueError("Signing requires RSA >= 2048 bits")
        self.private = private
        self.encryption = {
            key: base64.urlsafe_b64decode(value) for key, value in encryption["keys"].items()
        }
        if self.enc_kid not in self.encryption or any(
            len(k) != 32 for k in self.encryption.values()
        ):
            raise ValueError("Encryption requires a current 256-bit key")
        public: list[dict[str, Any]] = []
        for kid, pem in signing["keys"].items():
            if "PRIVATE KEY" in pem:
                key = serialization.load_pem_private_key(pem.encode(), None).public_key()
            else:
                key = serialization.load_pem_public_key(pem.encode())
            if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                raise ValueError("Published signing keys require RSA >= 2048 bits")
            entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key))
            entry.update(kid=kid, alg="RS256", use="sig")
            public.append(entry)
        self.jwks = {"keys": public}
        self.cache = JWKSCache(None, "RS256")
        self.cache.keys = {key["kid"]: key for key in public}
        self.cache.fetched_at = time.monotonic()

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def seal(self, value: str, purpose: str) -> str:
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.encryption[self.enc_kid]).encrypt(
            nonce, value.encode(), purpose.encode()
        )
        return self.enc_kid + "." + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def open(self, value: str, purpose: str) -> str:
        kid, encoded = value.split(".", 1)
        data = base64.urlsafe_b64decode(encoded)
        return AESGCM(self.encryption[kid]).decrypt(data[:12], data[12:], purpose.encode()).decode()

    def csrf(self, cookie: str) -> str:
        return hmac.new(
            self.encryption[self.enc_kid], b"csrf:" + cookie.encode(), "sha256"
        ).hexdigest()

    def issue(self, user: int, session_id: str, seconds: int, generation: int = 0) -> str:
        settings = get_settings()
        now = int(time.time())
        return jwt.encode(
            {
                "sub": str(user),
                "sid": session_id,
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "iat": now,
                "exp": now + seconds,
                "ver": generation,
            },
            self.private,
            algorithm="RS256",
            headers={"kid": self.kid},
        )
