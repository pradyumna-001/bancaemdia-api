"""Private pagination tokens bound to the owner, exact selection and projection."""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from bancaemdia.config import get_settings


def _context(owner: int, kind: str, filters: dict[str, object]) -> bytes:
    return json.dumps(
        {"v": 1, "owner": owner, "kind": kind, "filters": filters},
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: (
            value.astimezone(UTC).isoformat() if isinstance(value, datetime) else str(value)
        ),
    ).encode()


@lru_cache(maxsize=1)
def _cipher() -> AESGCM:
    configured = get_settings().PAINEL_CURSOR_SECRET
    secret = (
        configured.get_secret_value().encode()
        if configured
        else get_settings().COLETA_TOKEN_SECRET.encode()
    )
    key = hmac.new(secret, b"painel-pagination-v1", hashlib.sha256).digest()
    return AESGCM(key)


def encode_cursor(owner: int, kind: str, filters: dict[str, object], value: dict[str, Any]) -> str:
    payload = json.dumps(
        {"v": 1, "expires": int(datetime.now(UTC).timestamp()) + 86400, **value},
        separators=(",", ":"),
    ).encode()
    nonce = secrets.token_bytes(12)
    encrypted = _cipher().encrypt(nonce, payload, _context(owner, kind, filters))
    return base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")


def decode_cursor(
    token: str | None, owner: int, kind: str, filters: dict[str, object]
) -> dict[str, Any] | None:
    if token is None:
        return None
    try:
        packed = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(
            _cipher().decrypt(packed[:12], packed[12:], _context(owner, kind, filters))
        )
        if (
            not isinstance(value, dict)
            or value.get("v") != 1
            or type(value.get("expires")) is not int
            or value["expires"] < int(datetime.now(UTC).timestamp())
        ):
            raise ValueError("invalid cursor")
        return value
    except (InvalidTag, ValueError, TypeError, KeyError):
        raise HTTPException(422, "cursor inválido, expirado ou de outro recorte") from None
