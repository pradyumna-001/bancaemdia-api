import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import asyncpg.exceptions
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.config import get_settings
from bancaemdia.domain.event_bus import ApostaCriada, get_event_bus
from bancaemdia.domain.financeiro import Aposta as ApostaFinanceira
from bancaemdia.domain.materializar import (
    CupomLido,
    EventoNovo,
    LeituraRecebida,
    NovaAposta,
    apostas_da_leitura,
    eventos_da_releitura,
    projetar,
)
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.extracao.modelos import ExtracaoBilhete
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.workers.celery_app import app

MOTIVO_GRAVE_PADRAO = "conferência grave"
FUSO_DO_BRASIL = timezone(timedelta(hours=-3))

materialization_duration = Histogram(
    "materialization_duration_seconds", "Seconds to materialize one extraction reading"
)
materialization_failures = Counter(
    "materialization_failed", "Extraction readings that failed to materialize", ["reason"]
)


class GravacaoConcorrenteError(Exception):
    pass


RETRY_ON = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.CannotConnectNowError,
    GravacaoConcorrenteError,
)


class CupomDoJson(BaseModel):
    bilhete: ExtracaoBilhete
    motivo: str | None = None
    grave: bool = False


class ExtracaoDoJson(BaseModel):
    chat_id: int
    message_id: int
    postada_em: str | None = None
    bilhete: ExtracaoBilhete | None = None
    motivo: str | None = None
    grave: bool = False
    cupons: list[CupomDoJson] = Field(default_factory=list)
    nao_e_aposta: bool = False

    @field_validator("postada_em")
    @classmethod
    def postada_em_iso(cls, valor: str | None) -> str | None:
        if valor is not None:
            datetime.fromisoformat(valor)
        return valor

    def para_leitura(self) -> LeituraRecebida:
        return LeituraRecebida(
            bilhete=None if self.bilhete is None else self.bilhete.para_bilhete(),
            motivo=self.motivo,
            grave=self.grave,
            cupons=tuple(
                CupomLido(c.bilhete.para_bilhete(), c.motivo, c.grave) for c in self.cupons
            ),
            nao_e_aposta=self.nao_e_aposta,
        )


@dataclass(frozen=True)
class Gravada:
    chave: str
    origem: str
    criada: bool
    eventos: int
    revisao: bool
    revisao_grave: bool


@lru_cache
def get_engine() -> AsyncEngine:
    # Cada tarefa roda num `asyncio.run` novo, e conexão do asyncpg que ficou num laço anterior
    # quebra na tarefa seguinte: sem pool, cada tarefa abre e fecha a sua.
    return create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)


def _data(texto: str | None) -> datetime | None:
    if not texto:
        return None
    data = datetime.fromisoformat(texto)
    # O export do Telegram traz a hora local de quem postou, sem fuso, e o Brasil não tem horário
    # de verão desde 2019. Sem fuso explícito, o asyncpg usaria o da máquina do trabalhador.
    return data if data.tzinfo else data.replace(tzinfo=FUSO_DO_BRASIL)


async def _set_current_user(session: AsyncSession, usuario_id: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario_id)}
    )


async def _gravar(
    session: AsyncSession,
    usuario_id: int,
    nova: NovaAposta,
    extracao_json: dict[str, Any],
    midia_hash: str | None,
) -> Gravada:
    await _set_current_user(session, usuario_id)
    # Entrega "pelo menos uma vez": duas cópias da mesma aposta não podem decidir juntas que
    # ela é nova.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"{usuario_id}:{nova.chave}"},
    )
    eventos = EventoRepo()
    historico = [
        (e.tipo, e.fonte, e.payload_json)
        for e in await eventos.list_by_aposta_chave(session, usuario_id, nova.chave)
    ]
    atual, protegidos = projetar(historico)
    criada = not any(tipo == "APOSTA_CRIADA" for tipo, _, _ in historico)
    novos = (
        [EventoNovo("APOSTA_CRIADA", "ia", nova.payload, nova.confianca)]
        if criada
        else eventos_da_releitura(nova, atual, protegidos)
    )
    for evento in novos:
        da_mensagem = evento.tipo == "APOSTA_CRIADA"
        await eventos.append(
            session,
            {
                "usuario_id": usuario_id,
                "tipo": evento.tipo,
                "fonte": evento.fonte,
                "payload_json": evento.payload,
                "confianca": evento.confianca,
                "chat_id": nova.chat_id if da_mensagem else None,
                "message_id": nova.message_id if da_mensagem else None,
                "aposta_chave": nova.chave,
            },
        )

    estado, _ = projetar([*historico, *((e.tipo, e.fonte, e.payload) for e in novos)])
    data_aposta = _data(estado.get("data_aposta"))
    financeira = ApostaFinanceira(
        stake_unidades=float(estado.get("stake_unidades") or 0.0),
        valor_unidade_centavos=int(
            estado.get("valor_unidade_centavos") or VALOR_UNIDADE_PADRAO_CENTAVOS
        ),
        freebet=bool(estado.get("freebet")),
    )
    dados: dict[str, object] = {
        "usuario_id": usuario_id,
        "chave": nova.chave,
        "origem": nova.origem,
        "chat_id": nova.chat_id,
        "message_id": nova.message_id,
        "ordem_na_mensagem": nova.ordem,
        "data_aposta": data_aposta,
        "stake_unidades": financeira.stake_unidades,
        "stake_centavos": financeira.stake_centavos,
        "valor_aposta_centavos": financeira.valor_aposta_centavos,
        "odd": estado.get("odd"),
        "freebet": financeira.freebet,
        "revisao_grave": bool(estado.get("revisao_grave")),
    }
    if midia_hash is not None:
        dados["midia_hash"] = midia_hash
    conta_casa_id = estado.get("conta_casa_id")
    if conta_casa_id is None and estado.get("casa"):
        conta = await ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, usuario_id, estado["casa"], data_aposta
        )
        conta_casa_id = None if conta is None else conta.id
    if conta_casa_id is not None or criada or any("casa" in e.payload for e in novos):
        dados["conta_casa_id"] = conta_casa_id
    aposta = await ApostaRepo().upsert_materializada(session, dados)
    if aposta is None:
        raise GravacaoConcorrenteError(f"outra gravação mais nova de {nova.chave} chegou antes")

    grave = bool(estado.get("revisao_grave"))
    motivo = (estado.get("revisao_motivo") or MOTIVO_GRAVE_PADRAO) if grave else None
    motivo_mudou = any(e.tipo == "CORRECAO_MANUAL" and "revisao_motivo" in e.payload for e in novos)
    revisoes = RevisaoPendenteRepo()
    if motivo_mudou:
        # Como no projeto antigo, a fila segue a leitura: motivo novo substitui o velho, e a aposta
        # que ficou limpa sai da fila.
        await revisoes.resolve_superseded(session, usuario_id, nova.chave, motivo)
    revisao = False
    if (
        motivo is not None
        and (criada or motivo_mudou)
        and not await revisoes.has_open(session, usuario_id, nova.chave, motivo)
    ):
        await revisoes.create(
            session,
            {
                "usuario_id": usuario_id,
                "midia_hash": midia_hash,
                "motivo": motivo,
                "extracao_bruta": {"aposta_chave": nova.chave, "extracao": extracao_json},
            },
        )
        revisao = True
    return Gravada(nova.chave, nova.origem, criada, len(novos), revisao, grave)


async def gravar_leitura(
    engine: AsyncEngine,
    usuario_id: int,
    extracao: ExtracaoDoJson,
    extracao_json: dict[str, Any],
    midia_hash: str | None = None,
) -> list[Gravada]:
    gravadas: list[Gravada] = []
    async with (
        engine.connect() as conexao,
        AsyncSession(bind=conexao, expire_on_commit=False) as session,
    ):
        valor_unidade = VALOR_UNIDADE_PADRAO_CENTAVOS
        postada_em = _data(extracao.postada_em)
        if postada_em is not None:
            async with session.begin():
                await _set_current_user(session, usuario_id)
                unidade = await UnidadeRepo().get_vigente(session, usuario_id, postada_em)
            if unidade is not None:
                valor_unidade = unidade.valor_centavos
        novas = apostas_da_leitura(
            extracao.para_leitura(),
            chat_id=extracao.chat_id,
            message_id=extracao.message_id,
            data=extracao.postada_em,
            valor_unidade_centavos=valor_unidade,
        )
        for nova in novas:
            async with session.begin():
                gravada = await _gravar(session, usuario_id, nova, extracao_json, midia_hash)
            gravadas.append(gravada)
            if gravada.criada:
                get_event_bus().publish(
                    ApostaCriada(usuario_id, gravada.chave, gravada.origem, gravada.revisao_grave)
                )
    return gravadas


def materializar_aposta(
    usuario_id: int, extracao_json: dict[str, Any], midia_hash: str | None = None
) -> dict[str, object]:
    with materialization_duration.time():
        try:
            extracao = ExtracaoDoJson.model_validate(extracao_json)
        except ValidationError:
            materialization_failures.labels(reason="payload_invalido").inc()
            raise
        try:
            gravadas = asyncio.run(
                gravar_leitura(get_engine(), usuario_id, extracao, extracao_json, midia_hash)
            )
        except RETRY_ON:
            materialization_failures.labels(reason="banco").inc()
            raise
        except Exception:
            materialization_failures.labels(reason="inesperado").inc()
            raise
    return {
        "usuario_id": usuario_id,
        "apostas": [g.chave for g in gravadas],
        "criadas": sum(g.criada for g in gravadas),
        "eventos": sum(g.eventos for g in gravadas),
        "revisoes": sum(g.revisao for g in gravadas),
    }


materializar_aposta_task = app.task(
    name="materialization.materializar_aposta",
    autoretry_for=RETRY_ON,
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=False,
    max_retries=3,
)(materializar_aposta)
