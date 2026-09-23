import hashlib
import re
import unicodedata
from contextlib import suppress
from functools import lru_cache

import redis
from pydantic import BaseModel, ValidationError

from bancaemdia.cache.redis_client import sync_client
from bancaemdia.config import get_settings
from bancaemdia.extracao.cliente import VERSAO_PROMPT, Leitura
from bancaemdia.extracao.modelos import ExtracaoBilhete
from bancaemdia.observability.metrics import cache_errors, cache_hits, cache_misses

PREFIXO = "ext"
TTL_SEGUNDOS = 30 * 86400
TIMEOUT_SEGUNDOS = 2.0
LOTE_DE_LIMPEZA = 500
RE_VERSAO = re.compile(r"^(.*)_v(\d+)$")


class LeituraGuardada(BaseModel):
    cupons: list[ExtracaoBilhete]
    modelo: str


def normalizar_legenda(legenda: str) -> str:
    limpa = unicodedata.normalize("NFC", legenda).strip().lower()
    return re.sub(r"\s+", " ", limpa)


def chave_de_imagem(imagem: bytes, legenda: str = "") -> str:
    hash_da_imagem = hashlib.sha256(imagem).hexdigest()
    if not legenda.strip():
        return hash_da_imagem
    return hashlib.sha256(f"{hash_da_imagem}|{normalizar_legenda(legenda)}".encode()).hexdigest()


def chave_no_redis(chave: str, versao_prompt: str) -> str:
    return f"{PREFIXO}:{chave}:{versao_prompt}"


def versao_anterior(versao: str, atual: str) -> bool:
    lida = RE_VERSAO.match(versao)
    corrente = RE_VERSAO.match(atual)
    if lida is None or corrente is None or lida.group(1) != corrente.group(1):
        return False
    return int(lida.group(2)) < int(corrente.group(2))


class ExtracaoCache:
    def __init__(
        self,
        client: redis.Redis,
        versao_prompt: str = VERSAO_PROMPT,
        ttl_segundos: int = TTL_SEGUNDOS,
    ) -> None:
        self.client = client
        self.versao_prompt = versao_prompt
        self.ttl_segundos = ttl_segundos

    def buscar(self, chave: str) -> LeituraGuardada | None:
        nome = chave_no_redis(chave, self.versao_prompt)
        try:
            bruto = self.client.get(nome)
        except redis.RedisError:
            cache_errors.inc()
            bruto = None
        if bruto is None:
            cache_misses.inc()
            return None
        try:
            guardada = LeituraGuardada.model_validate_json(bruto)
        except ValidationError:
            cache_errors.inc()
            cache_misses.inc()
            with suppress(redis.RedisError):
                self.client.delete(nome)
            return None
        cache_hits.inc()
        return guardada

    def guardar(self, chave: str, leitura: Leitura, *, substituir: bool = True) -> None:
        guardada = LeituraGuardada(cupons=list(leitura.bilhetes), modelo=leitura.modelo)
        try:
            self.client.set(
                chave_no_redis(chave, self.versao_prompt),
                guardada.model_dump_json(),
                ex=self.ttl_segundos,
                nx=not substituir,
            )
        except redis.RedisError:
            cache_errors.inc()

    def limpar_versoes_antigas(self) -> int:
        apagadas = 0
        lote: list[str] = []
        try:
            for nome in self.client.scan_iter(match=f"{PREFIXO}:*", count=LOTE_DE_LIMPEZA):
                texto = nome.decode() if isinstance(nome, bytes) else str(nome)
                if versao_anterior(texto.rsplit(":", 1)[-1], self.versao_prompt):
                    lote.append(texto)
                if len(lote) == LOTE_DE_LIMPEZA:
                    apagadas += sum(self.client.unlink(nome) for nome in lote)
                    lote = []
            if lote:
                apagadas += sum(self.client.unlink(nome) for nome in lote)
        except redis.RedisError:
            cache_errors.inc()
        return apagadas


@lru_cache
def get_cache() -> ExtracaoCache:
    settings = get_settings()
    client = sync_client(
        settings.REDIS_URL, cluster=settings.REDIS_CLUSTER_MODE, timeout=TIMEOUT_SEGUNDOS
    )
    return ExtracaoCache(client)
