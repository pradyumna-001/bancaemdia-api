import asyncio
import re
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import httpx
import jwt
import structlog
from jwt.types import Options

from bancaemdia.config import get_settings

TIMEOUT_SECONDS = 2.0
JWKS_TTL_SECONDS = 300
REFETCH_COOLDOWN_SECONDS = 30
BIGINT_MAX = 2**63 - 1
SUBJECT = re.compile(r"[1-9][0-9]{0,18}")
REQUIRED_CLAIMS: Options = {"require": ["exp", "aud", "iss", "sub"]}


class InvalidTokenError(Exception):
    pass


class KeysUnavailableError(Exception):
    pass


def rsa_signing_keys(jwks: object, algorithm: str) -> dict[str, dict[str, Any]] | None:
    entries = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(entries, list):
        return None
    # Seleciona só chaves RSA de assinatura com o algoritmo configurado.
    keys: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("kid"), str)
            and entry.get("kty") == "RSA"
            and entry.get("use", "sig") == "sig"
            and entry.get("alg", algorithm) == algorithm
        ):
            continue
        # Descarta chaves malformadas antes de guardá-las no cache.
        try:
            parsed = jwt.PyJWK.from_dict(entry, algorithm=algorithm)
            if parsed.key.key_size < 2048:
                raise ValueError("RSA signing key is too short")
        except (jwt.PyJWTError, ValueError, TypeError):
            structlog.get_logger().warning("jwks_key_skipped", kid=entry["kid"])
            continue
        keys[entry["kid"]] = entry
    return keys


class JWKSCache:
    def __init__(
        self,
        url: str | None,
        algorithm: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.algorithm = algorithm
        self.transport = transport
        self.clock = clock
        self.keys: dict[str, dict[str, Any]] = {}
        self.fetched_at: float | None = None
        self.tried_at: float | None = None
        self.lock = asyncio.Lock()

    async def key_for(self, kid: str) -> dict[str, Any]:
        now = self.clock()
        fresh = self.fetched_at is not None and now - self.fetched_at < JWKS_TTL_SECONDS
        if kid in self.keys and fresh:
            return self.keys[kid]
        # Quem chega durante uma busca espera por ela: sem a trava, pedidos simultâneos com um token
        # bom recebiam 503 ou 401 enquanto a primeira busca ainda estava no ar (medido).
        async with self.lock:
            # Um `kid` inventado em cada pedido não pode virar uma busca por pedido no servidor de chaves.
            if self.tried_at is None or now - self.tried_at >= REFETCH_COOLDOWN_SECONDS:
                self.tried_at = now
                await self._refresh(now)
        if kid in self.keys:
            return self.keys[kid]
        if not self.keys:
            raise KeysUnavailableError("no signing keys could be loaded from the JWKS endpoint")
        raise InvalidTokenError("the token was signed with an unknown key")

    async def _refresh(self, now: float) -> None:
        if self.url is None:
            return
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, transport=self.transport
            ) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                keys = rsa_signing_keys(response.json(), self.algorithm)
        # InvalidURL não é HTTPError, e um corpo aninhado demais para o json vira RecursionError.
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, RecursionError) as error:
            keys = None
            structlog.get_logger().warning(
                "jwks_fetch_failed", url=self.url, error=type(error).__name__
            )
        # Com o servidor de chaves fora do ar, as chaves já carregadas continuam valendo.
        if keys is not None:
            self.keys = keys
            self.fetched_at = now


async def verify_token(token: str, cache: JWKSCache) -> int:
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        raise InvalidTokenError("the token is malformed") from error
    kid = header.get("kid")
    if header.get("alg") != settings.JWT_ALGORITHM or not isinstance(kid, str):
        raise InvalidTokenError("the token header is not accepted")
    key = await cache.key_for(kid)
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            jwt.PyJWK.from_dict(key, algorithm=settings.JWT_ALGORITHM).key,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options=REQUIRED_CLAIMS,
        )
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError) as error:
        raise InvalidTokenError("the token is not valid") from error
    subject = claims["sub"]
    # O RLS compara `app.current_user_id` com usuario_id bigint: um sub que não cabe viraria erro do
    # banco, não 401.
    if not isinstance(subject, str) or not SUBJECT.fullmatch(subject) or int(subject) > BIGINT_MAX:
        raise InvalidTokenError("the token subject is not a user id")
    return int(subject)


@lru_cache
def get_jwks_cache() -> JWKSCache:
    settings = get_settings()
    return JWKSCache(settings.JWT_JWKS_URL, settings.JWT_ALGORITHM)
