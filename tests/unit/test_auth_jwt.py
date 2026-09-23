from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.config import get_settings

URL = "https://issuer.test/.well-known/jwks.json"


def _par(kid="k1"):
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = chave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    publica = chave.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return (
        privada,
        publica.decode(),
        {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(chave.public_key())),
            "kid": kid,
        },
    )


@pytest.fixture(scope="module")
def k1():
    return _par("k1")


@pytest.fixture(scope="module")
def k2():
    return _par("k2")


def _claims(**claims):
    settings = get_settings()
    return {
        "sub": "7",
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "exp": int(time.time()) + 60,
        **claims,
    }


def _token(privada, kid="k1", **claims):
    corpo = {nome: valor for nome, valor in _claims(**claims).items() if valor is not None}
    return jwt.encode(corpo, privada, algorithm="RS256", headers={"kid": kid})


def _servidor(*chaves):
    class Servidor:
        def __init__(self):
            self.chaves = list(chaves)
            self.buscas = 0
            self.fora = False
            self.corpo = None

        def responder(self, request):
            self.buscas += 1
            if self.fora:
                raise httpx.ConnectError("refused", request=request)
            if self.corpo is not None:
                return httpx.Response(200, content=self.corpo)
            return httpx.Response(200, json={"keys": self.chaves})

    return Servidor()


def _relogio():
    class Relogio:
        def __init__(self):
            self.agora = 1000.0

        def __call__(self):
            return self.agora

    return Relogio()


def _cache(servidor, relogio=None):
    return auth_jwt.JWKSCache(
        URL,
        "RS256",
        transport=httpx.MockTransport(servidor.responder),
        clock=relogio or _relogio(),
    )


def _b64(dados):
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


async def test_valid_token_gives_the_user_id(k1) -> None:
    privada, _, chave = k1
    servidor = _servidor(chave)

    usuario_id = await auth_jwt.verify_token(_token(privada), _cache(servidor))

    assert usuario_id == 7


async def test_expired_token_is_invalid(k1) -> None:
    privada, _, chave = k1

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(
            _token(privada, exp=int(time.time()) - 1), _cache(_servidor(chave))
        )


@pytest.mark.parametrize(("claim", "valor"), [("aud", "other-api"), ("iss", "other-issuer")])
async def test_token_for_another_audience_or_issuer_is_invalid(k1, claim, valor) -> None:
    privada, _, chave = k1

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(_token(privada, **{claim: valor}), _cache(_servidor(chave)))


@pytest.mark.parametrize("claim", ["aud", "exp", "iss", "sub"])
async def test_token_missing_a_required_claim_is_invalid(k1, claim) -> None:
    privada, _, chave = k1

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(_token(privada, **{claim: None}), _cache(_servidor(chave)))


@pytest.mark.parametrize("sub", ["abc", "0", "-1", "07", "1.5", "٣", "9223372036854775808", 7])
async def test_subject_must_be_a_positive_bigint_user_id(k1, sub) -> None:
    privada, _, chave = k1

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(_token(privada, sub=sub), _cache(_servidor(chave)))


@pytest.mark.parametrize(
    "claims", [{"exp": None}, {"exp": [1]}, {"exp": float("inf")}, {"nbf": {}}, {"iat": []}]
)
async def test_signed_token_with_a_mistyped_time_claim_is_invalid_not_a_crash(k1, claims) -> None:
    privada, _, chave = k1
    token = jwt.encode(_claims(**claims), privada, algorithm="RS256", headers={"kid": "k1"})

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(token, _cache(_servidor(chave)))


async def test_largest_bigint_subject_is_accepted(k1) -> None:
    privada, _, chave = k1

    usuario_id = await auth_jwt.verify_token(
        _token(privada, sub="9223372036854775807"), _cache(_servidor(chave))
    )

    assert usuario_id == 2**63 - 1


async def test_hs256_token_signed_with_the_public_key_is_invalid(k1) -> None:
    _, publica, chave = k1
    servidor = _servidor(chave)
    cabecalho = _b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    corpo = _b64(json.dumps(_claims()).encode())
    assinatura = _b64(
        hmac.new(publica.encode(), f"{cabecalho}.{corpo}".encode(), hashlib.sha256).digest()
    )

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(f"{cabecalho}.{corpo}.{assinatura}", _cache(servidor))
    assert servidor.buscas == 0


async def test_unsigned_malformed_or_kid_less_tokens_are_invalid(k1) -> None:
    privada, _, chave = k1
    servidor = _servidor(chave)
    sem_assinatura = (
        _b64(json.dumps({"alg": "none", "kid": "k1"}).encode()) + "." + _b64(b"{}") + "."
    )
    sem_kid = jwt.encode(_claims(), privada, algorithm="RS256")

    for token in (sem_assinatura, "not-a-token", sem_kid):
        with pytest.raises(auth_jwt.InvalidTokenError):
            await auth_jwt.verify_token(token, _cache(servidor))
    assert servidor.buscas == 0


@pytest.mark.parametrize(
    "estrago", [{"n": "não é base64"}, {"n": "!!!"}, {"n": 123}, {"e": ""}, {"n": None}]
)
async def test_malformed_published_keys_are_skipped_instead_of_crashing(k1, k2, estrago) -> None:
    privada1, _, chave1 = k1
    privada2, _, chave2 = k2
    quebrada = {**chave1, **estrago}

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(_token(privada1), _cache(_servidor(quebrada, chave2)))
    with pytest.raises(auth_jwt.KeysUnavailableError):
        await auth_jwt.verify_token(_token(privada1), _cache(_servidor(quebrada)))
    assert (
        await auth_jwt.verify_token(_token(privada2, kid="k2"), _cache(_servidor(quebrada, chave2)))
        == 7
    )


async def test_only_rsa_signing_keys_for_the_configured_algorithm_are_used(k1) -> None:
    privada, _, chave = k1
    curva = ec.generate_private_key(ec.SECP256R1()).public_key()
    outra = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(curva))
    servidor = _servidor(
        {**outra, "kid": "k1"},
        {**chave, "use": "enc", "kid": "k8"},
        {**chave, "alg": "RS512", "kid": "k9"},
        chave,
    )

    usuario_id = await auth_jwt.verify_token(_token(privada), _cache(servidor))

    assert usuario_id == 7
    assert auth_jwt.rsa_signing_keys({"keys": servidor.chaves}, "RS256") == {"k1": chave}
    assert auth_jwt.rsa_signing_keys({"chaves": []}, "RS256") is None


async def test_keys_are_fetched_once_while_fresh(k1) -> None:
    privada, _, chave = k1
    servidor = _servidor(chave)
    relogio = _relogio()
    cache = _cache(servidor, relogio)

    await auth_jwt.verify_token(_token(privada), cache)
    relogio.agora += auth_jwt.JWKS_TTL_SECONDS - 1
    await auth_jwt.verify_token(_token(privada), cache)

    assert servidor.buscas == 1


async def test_unknown_kid_refetches_once_per_cooldown(k1) -> None:
    privada, _, chave = k1
    servidor = _servidor(chave)
    relogio = _relogio()
    cache = _cache(servidor, relogio)
    await auth_jwt.verify_token(_token(privada), cache)

    relogio.agora += auth_jwt.REFETCH_COOLDOWN_SECONDS
    for _ in range(3):
        with pytest.raises(auth_jwt.InvalidTokenError):
            await auth_jwt.verify_token(_token(privada, kid="inventado"), cache)
    buscas_no_intervalo = servidor.buscas
    relogio.agora += auth_jwt.REFETCH_COOLDOWN_SECONDS
    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(_token(privada, kid="inventado"), cache)

    assert (buscas_no_intervalo, servidor.buscas) == (2, 3)


async def test_rotated_key_is_accepted_after_one_refetch(k1, k2) -> None:
    privada1, _, chave1 = k1
    privada2, _, chave2 = k2
    servidor = _servidor(chave1)
    relogio = _relogio()
    cache = _cache(servidor, relogio)
    await auth_jwt.verify_token(_token(privada1), cache)

    servidor.chaves = [chave1, chave2]
    relogio.agora += auth_jwt.REFETCH_COOLDOWN_SECONDS

    assert await auth_jwt.verify_token(_token(privada2, kid="k2"), cache) == 7
    assert await auth_jwt.verify_token(_token(privada1), cache) == 7
    assert servidor.buscas == 2


async def test_removed_key_stops_working_once_the_keys_expire(k1, k2) -> None:
    privada1, _, chave1 = k1
    _, _, chave2 = k2
    servidor = _servidor(chave1)
    relogio = _relogio()
    cache = _cache(servidor, relogio)
    await auth_jwt.verify_token(_token(privada1), cache)

    servidor.chaves = [chave2]
    relogio.agora += auth_jwt.JWKS_TTL_SECONDS

    with pytest.raises(auth_jwt.InvalidTokenError):
        await auth_jwt.verify_token(_token(privada1), cache)


async def test_loaded_keys_keep_working_while_the_key_server_is_down(k1) -> None:
    privada, _, chave = k1
    servidor = _servidor(chave)
    relogio = _relogio()
    cache = _cache(servidor, relogio)
    await auth_jwt.verify_token(_token(privada), cache)

    servidor.fora = True
    relogio.agora += auth_jwt.JWKS_TTL_SECONDS
    fora = await auth_jwt.verify_token(_token(privada), cache)
    servidor.fora = False
    servidor.corpo = b"<html>maintenance</html>"
    relogio.agora += auth_jwt.REFETCH_COOLDOWN_SECONDS
    quebrado = await auth_jwt.verify_token(_token(privada), cache)

    assert (fora, quebrado, servidor.buscas) == (7, 7, 3)


async def test_without_any_loaded_key_authentication_is_unavailable(k1) -> None:
    privada, _, chave = k1
    fora = _servidor(chave)
    fora.fora = True
    aninhado = _servidor(chave)
    aninhado.corpo = b"[" * 100_000 + b"]" * 100_000

    for cache in (
        _cache(fora),
        _cache(aninhado),
        auth_jwt.JWKSCache(None, "RS256"),
        auth_jwt.JWKSCache("https://[::1/jwks", "RS256"),
    ):
        with pytest.raises(auth_jwt.KeysUnavailableError):
            await auth_jwt.verify_token(_token(privada), cache)


def _servidor_lento(*chaves):
    class Servidor:
        def __init__(self):
            self.chaves = list(chaves)
            self.buscas = 0

        async def responder(self, request):
            self.buscas += 1
            await asyncio.sleep(0.2)
            return httpx.Response(200, json={"keys": self.chaves})

    return Servidor()


async def test_concurrent_requests_wait_for_the_fetch_in_flight(k1, k2) -> None:
    privada1, _, chave1 = k1
    privada2, _, chave2 = k2
    servidor = _servidor_lento(chave1)
    relogio = _relogio()
    cache = _cache(servidor, relogio)

    frio = await asyncio.gather(
        *(auth_jwt.verify_token(_token(privada1), cache) for _ in range(5)),
        return_exceptions=True,
    )
    servidor.chaves = [chave1, chave2]
    relogio.agora += auth_jwt.REFETCH_COOLDOWN_SECONDS
    rotacao = await asyncio.gather(
        *(auth_jwt.verify_token(_token(privada2, kid="k2"), cache) for _ in range(5)),
        return_exceptions=True,
    )

    assert (frio, rotacao, servidor.buscas) == ([7] * 5, [7] * 5, 2)


def test_the_process_cache_reads_the_jwks_url_from_the_settings(monkeypatch) -> None:
    monkeypatch.setenv("JWT_JWKS_URL", URL)
    get_settings.cache_clear()
    auth_jwt.get_jwks_cache.cache_clear()
    try:
        cache = auth_jwt.get_jwks_cache()
        assert (cache.url, cache.algorithm) == (URL, "RS256")
        assert auth_jwt.get_jwks_cache() is cache
    finally:
        get_settings.cache_clear()
        auth_jwt.get_jwks_cache.cache_clear()
