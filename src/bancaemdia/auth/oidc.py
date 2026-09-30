"""Authorization Code / S256 client. Endpoints come only from the configured issuer."""

import asyncio
import hashlib
import math
import secrets
import time
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from pydantic import EmailStr, TypeAdapter

from bancaemdia.auth.identity_config import IdentitySettings
from bancaemdia.auth.jwt import InvalidTokenError, JWKSCache, KeysUnavailableError


class IdentityError(Exception):
    def __init__(self, code: str, status: int = 401) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class OIDCClient:
    def __init__(self, settings: IdentitySettings) -> None:
        self.settings = settings
        self.document: dict[str, Any] | None = None
        self.document_at = 0.0
        self.lock = asyncio.Lock()
        self.cache: JWKSCache | None = None

    async def discovery(self) -> dict[str, Any]:
        async with self.lock:
            if self.document is not None and time.monotonic() - self.document_at < 300:
                return self.document
            try:
                async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                    response = await client.get(
                        self.settings.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
                    )
                    response.raise_for_status()
                    document = response.json()
                if document.get("issuer") != self.settings.OIDC_ISSUER:
                    raise ValueError("issuer")
                if self.settings.OIDC_REVOCATION_ENDPOINT is not None:
                    document["revocation_endpoint"] = self.settings.OIDC_REVOCATION_ENDPOINT
                issuer = urlsplit(self.settings.OIDC_ISSUER)
                for key in (
                    "authorization_endpoint",
                    "token_endpoint",
                    "jwks_uri",
                    "revocation_endpoint",
                ):
                    value = document.get(key)
                    if not isinstance(value, str):
                        raise ValueError("endpoint")
                    url = urlsplit(value)
                    if (
                        url.username
                        or url.password
                        or url.fragment
                        or url.query
                        or url.scheme != issuer.scheme
                        or not url.hostname
                        or (url.scheme == "http" and url.netloc != issuer.netloc)
                    ):
                        raise ValueError("endpoint")
                if "RS256" not in document.get("id_token_signing_alg_values_supported", []):
                    raise ValueError("algorithm")
            except (
                httpx.HTTPError,
                httpx.InvalidURL,
                ValueError,
                TypeError,
                AttributeError,
                RecursionError,
            ) as error:
                raise IdentityError("issuer_unavailable", 503) from error
            self.document = cast(dict[str, Any], document)
            self.document_at = time.monotonic()
            self.cache = JWKSCache(document["jwks_uri"], "RS256")
            return self.document

    async def authorization(self, state: str, nonce: str, verifier: str) -> str:
        import base64

        document = await self.discovery()
        challenge = (
            base64
            .urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return (
            str(document["authorization_endpoint"])
            + "?"
            + urlencode({
                "response_type": "code",
                "client_id": self.settings.OIDC_CLIENT_ID,
                "redirect_uri": self.settings.callback,
                "scope": self.settings.OIDC_SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
            })
        )

    async def tokens(self, data: dict[str, str]) -> dict[str, Any]:
        document = await self.discovery()
        data = {**data, "client_id": self.settings.OIDC_CLIENT_ID}
        secret = self.settings.OIDC_CLIENT_SECRET
        auth = (
            httpx.USE_CLIENT_DEFAULT
            if secret is None
            else httpx.BasicAuth(self.settings.OIDC_CLIENT_ID, secret.get_secret_value())
        )
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.post(document["token_endpoint"], data=data, auth=auth)
                if response.status_code in {400, 401}:
                    raise IdentityError("identity_rejected")
                response.raise_for_status()
                result = response.json()
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("id_token"), str)
                or result.get("token_type", "").lower() != "bearer"
            ):
                raise ValueError("tokens")
            return result
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, RecursionError) as error:
            raise IdentityError("issuer_unavailable", 503) from error

    async def identity(self, token: str, nonce: str | None) -> dict[str, Any]:
        await self.discovery()
        assert self.cache is not None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("header")
            key = await self.cache.key_for(header["kid"])
            if self.cache.fetched_at is None or time.monotonic() - self.cache.fetched_at >= 300:
                raise IdentityError("issuer_unavailable", 503)
            claims: dict[str, Any] = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                audience=self.settings.OIDC_CLIENT_ID,
                issuer=self.settings.OIDC_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            if (
                not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 255
                or any(
                    not isinstance(claims[x], (int, float))
                    or isinstance(claims[x], bool)
                    or not math.isfinite(claims[x])
                    for x in ("exp", "iat")
                )
            ):
                raise ValueError("claims")
            audience = claims["aud"]
            if (
                (isinstance(audience, list) and len(audience) > 1) or "azp" in claims
            ) and claims.get("azp") != self.settings.OIDC_CLIENT_ID:
                raise ValueError("authorized party")
            if nonce is not None and (
                not isinstance(claims.get("nonce"), str)
                or not secrets.compare_digest(claims["nonce"], nonce)
            ):
                raise ValueError("nonce")
            if claims.get("email_verified") is not True:
                raise IdentityError("email_unconfirmed", 403)
            email = claims.get("email")
            if not isinstance(email, str) or len(email) > 254 or "@" not in email:
                raise ValueError("email")
            claims["email"] = TypeAdapter(EmailStr).validate_python(email)
            return claims
        except KeysUnavailableError as error:
            raise IdentityError("issuer_unavailable", 503) from error
        except (InvalidTokenError, jwt.PyJWTError, ValueError, TypeError, OverflowError) as error:
            raise IdentityError("identity_rejected") from error

    async def revoke(self, token: str) -> None:
        document = await self.discovery()
        data = {
            "token": token,
            "token_type_hint": "refresh_token",
            "client_id": self.settings.OIDC_CLIENT_ID,
        }
        secret = self.settings.OIDC_CLIENT_SECRET
        auth = (
            httpx.USE_CLIENT_DEFAULT
            if secret is None
            else httpx.BasicAuth(self.settings.OIDC_CLIENT_ID, secret.get_secret_value())
        )
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.post(document["revocation_endpoint"], data=data, auth=auth)
                response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise IdentityError("issuer_unavailable", 503) from error
