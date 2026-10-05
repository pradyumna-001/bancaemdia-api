"""Short lived, encrypted extraction cache isolated by authenticated tenant.

The same ladder/cache contract is used; bot data never enters the legacy global
plaintext cache. Redis TTL bounds recovery residue even when a worker crashes.
"""

import redis
from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from bancaemdia.cache.extracao_cache import ExtracaoCache, LeituraGuardada
from bancaemdia.extracao.cliente import Leitura
from bancaemdia.integrations.telegram.codec import _fernet
from bancaemdia.services.telegram_link import _digest


class TelegramExtractionCache(ExtracaoCache):
    def __init__(self, base: ExtracaoCache, user_id: int) -> None:
        super().__init__(base.client, base.versao_prompt, ttl_segundos=60)
        self.prefix = "tgext:" + _digest("cache", str(user_id)) + ":"

    def buscar(self, chave: str) -> LeituraGuardada | None:
        try:
            value = self.client.get(self.prefix + chave + ":" + self.versao_prompt)
            return (
                None
                if value is None
                else LeituraGuardada.model_validate_json(_fernet().decrypt(value))
            )
        except (redis.RedisError, InvalidToken, ValidationError):
            return None

    def guardar(self, chave: str, leitura: Leitura, *, substituir: bool = True) -> None:
        value = LeituraGuardada(cupons=list(leitura.bilhetes), modelo=leitura.modelo)
        try:
            self.client.set(
                self.prefix + chave + ":" + self.versao_prompt,
                _fernet().encrypt(value.model_dump_json().encode()),
                ex=self.ttl_segundos,
                nx=not substituir,
            )
        except redis.RedisError:
            # Optional cache failure never bypasses the separate shared AI limiter.
            return
