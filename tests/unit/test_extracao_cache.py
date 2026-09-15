from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest
import redis
from prometheus_client import REGISTRY

from bancaemdia.cache import extracao_cache
from bancaemdia.config import get_settings
from bancaemdia.extracao.cliente import VERSAO_PROMPT, Leitura
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao


def _redis(falha=None):
    class Redis:
        def __init__(self):
            self.dados = {}
            self.validade = {}

        def get(self, nome):
            if falha is not None:
                raise falha
            return self.dados.get(nome)

        def set(self, nome, valor, ex=None, nx=False):
            if falha is not None:
                raise falha
            if nx and nome in self.dados:
                return None
            self.dados[nome] = valor.encode() if isinstance(valor, str) else valor
            self.validade[nome] = ex
            return True

        def scan_iter(self, match=None, count=None):
            if falha is not None:
                raise falha
            prefixo = match.rstrip("*")
            return iter([n.encode() for n in list(self.dados) if n.startswith(prefixo)])

        def delete(self, *nomes):
            return sum(self.dados.pop(n, None) is not None for n in nomes)

        def unlink(self, *nomes):
            return self.delete(*nomes)

    return Redis()


def _bilhete():
    return ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    )


def _contagem(nome):
    return REGISTRY.get_sample_value(f"{nome}_total") or 0.0


def test_key_without_caption_is_the_image_hash() -> None:
    assert extracao_cache.chave_de_imagem(b"foto", "   ") == hashlib.sha256(b"foto").hexdigest()


def test_caption_enters_the_key() -> None:
    assert extracao_cache.chave_de_imagem(b"foto", "odd 2.16") != extracao_cache.chave_de_imagem(
        b"foto", "odd 19.00"
    )


def test_caption_is_normalized_before_hashing() -> None:
    nfd = unicodedata.normalize("NFD", "1U  Grêmio\n")

    assert extracao_cache.chave_de_imagem(b"foto", nfd) == extracao_cache.chave_de_imagem(
        b"foto", "1u grêmio"
    )


def test_key_follows_the_original_formula() -> None:
    hash_da_foto = hashlib.sha256(b"foto").hexdigest()
    esperado = hashlib.sha256(f"{hash_da_foto}|1u betano".encode()).hexdigest()

    assert extracao_cache.chave_de_imagem(b"foto", " 1u\tBetano ") == esperado


def test_redis_key_carries_the_prompt_version() -> None:
    assert (
        extracao_cache.chave_no_redis("abc", "extrair_bilhete_v3") == "ext:abc:extrair_bilhete_v3"
    )


def test_stored_reading_comes_back_with_its_model_for_30_days() -> None:
    cliente = _redis()
    cache = extracao_cache.ExtracaoCache(cliente)

    cache.guardar("abc", Leitura((_bilhete(),), "claude-haiku-4-5", custo_usd=0.012))
    guardada = cache.buscar("abc")

    nome = f"ext:abc:{VERSAO_PROMPT}"
    assert guardada == extracao_cache.LeituraGuardada(
        cupons=[_bilhete()], modelo="claude-haiku-4-5"
    )
    assert cliente.validade == {nome: 30 * 86400}
    assert json.loads(cliente.dados[nome])["modelo"] == "claude-haiku-4-5"


def test_write_that_must_not_replace_keeps_the_existing_reading() -> None:
    cache = extracao_cache.ExtracaoCache(_redis())

    cache.guardar("abc", Leitura((_bilhete(),), "claude-sonnet-5"))
    cache.guardar("abc", Leitura((_bilhete(),), "claude-haiku-4-5"), substituir=False)

    assert cache.buscar("abc").modelo == "claude-sonnet-5"


def test_hits_and_misses_are_counted() -> None:
    cache = extracao_cache.ExtracaoCache(_redis())
    hits, misses = _contagem("extraction_cache_hit"), _contagem("extraction_cache_miss")

    assert cache.buscar("abc") is None
    cache.guardar("abc", Leitura((_bilhete(),), "claude-haiku-4-5"))
    assert cache.buscar("abc") is not None

    assert _contagem("extraction_cache_hit") == pytest.approx(hits + 1)
    assert _contagem("extraction_cache_miss") == pytest.approx(misses + 1)


def test_other_prompt_version_is_a_miss() -> None:
    cliente = _redis()
    extracao_cache.ExtracaoCache(cliente, versao_prompt="extrair_bilhete_v2").guardar(
        "abc", Leitura((_bilhete(),), "claude-haiku-4-5")
    )

    assert extracao_cache.ExtracaoCache(cliente).buscar("abc") is None


def test_corrupt_entry_is_a_miss_and_is_removed() -> None:
    cliente = _redis()
    nome = f"ext:abc:{VERSAO_PROMPT}"
    cliente.dados[nome] = b'{"cupons": "nope"}'
    erros = _contagem("extraction_cache_error")

    assert extracao_cache.ExtracaoCache(cliente).buscar("abc") is None
    assert nome not in cliente.dados
    assert _contagem("extraction_cache_error") == pytest.approx(erros + 1)


def test_redis_down_reads_as_a_miss_and_skips_writes() -> None:
    cache = extracao_cache.ExtracaoCache(_redis(falha=redis.ConnectionError("down")))
    misses, erros = _contagem("extraction_cache_miss"), _contagem("extraction_cache_error")

    assert cache.buscar("abc") is None
    cache.guardar("abc", Leitura((_bilhete(),), "claude-haiku-4-5"))
    assert cache.limpar_versoes_antigas() == 0
    assert _contagem("extraction_cache_miss") == pytest.approx(misses + 1)
    assert _contagem("extraction_cache_error") == pytest.approx(erros + 3)


@pytest.mark.parametrize(
    ("versao", "anterior"),
    [
        ("extrair_bilhete_v2", True),
        ("extrair_bilhete_v3", False),
        ("extrair_bilhete_v4", False),
        ("extrair_bilhete_v31", False),
        ("outro_prompt_v1", False),
        ("sem_numero", False),
    ],
)
def test_versao_anterior_only_recognizes_older_versions_of_the_same_prompt(
    versao, anterior
) -> None:
    assert extracao_cache.versao_anterior(versao, "extrair_bilhete_v3") is anterior


def test_limpar_versoes_antigas_never_deletes_newer_or_unknown_versions() -> None:
    cliente = _redis()
    manter = {
        "ext:a:extrair_bilhete_v3",
        "ext:b:extrair_bilhete_v4",
        "ext:c:outro_prompt_v1",
        "celery",
    }
    cliente.dados = dict.fromkeys(
        manter | {"ext:d:extrair_bilhete_v2", "ext:e:extrair_bilhete_v1"}, b"{}"
    )

    apagadas = extracao_cache.ExtracaoCache(
        cliente, versao_prompt="extrair_bilhete_v3"
    ).limpar_versoes_antigas()

    assert apagadas == 2
    assert set(cliente.dados) == manter


def test_limpar_versoes_antigas_deletes_in_batches(monkeypatch) -> None:
    cliente = _redis()
    cliente.dados = {f"ext:{i}:extrair_bilhete_v1": b"{}" for i in range(5)}
    lotes = []
    unlink = cliente.unlink
    cliente.unlink = lambda *nomes: lotes.append(len(nomes)) or unlink(*nomes)
    monkeypatch.setattr(extracao_cache, "LOTE_DE_LIMPEZA", 2)

    apagadas = extracao_cache.ExtracaoCache(
        cliente, versao_prompt="extrair_bilhete_v3"
    ).limpar_versoes_antigas()

    assert apagadas == 5
    assert lotes == [2, 2, 1]


def test_get_cache_reads_redis_url_with_short_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://cache-host:6380/2")
    get_settings.cache_clear()
    extracao_cache.get_cache.cache_clear()
    try:
        cache = extracao_cache.get_cache()
    finally:
        get_settings.cache_clear()
        extracao_cache.get_cache.cache_clear()

    opcoes = cache.client.connection_pool.connection_kwargs
    assert (opcoes["host"], opcoes["port"], opcoes["db"]) == ("cache-host", 6380, 2)
    assert opcoes["socket_timeout"] == extracao_cache.TIMEOUT_SEGUNDOS
    assert opcoes["socket_connect_timeout"] == extracao_cache.TIMEOUT_SEGUNDOS
    assert cache.versao_prompt == VERSAO_PROMPT
