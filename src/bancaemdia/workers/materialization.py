import asyncio
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import asyncpg.exceptions
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.coleta.leitores import LEITORES
from bancaemdia.config import get_settings
from bancaemdia.domain.coleta_casa import (
    casa_da_coleta,
    chave_casa,
    eventos_da_criacao,
    eventos_do_resultado,
    validar,
)
from bancaemdia.domain.conferencias import GRAVES, Bilhete, Origem, conferir
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
from bancaemdia.domain.registros import ColetaCasa
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.extracao.modelos import ExtracaoBilhete
from bancaemdia.observability.metrics import (
    batch_bets_processed,
    materialization_duration,
    materialization_failures,
    observe_stage,
    revisao_pendente_created,
)
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.workers.celery_app import MATERIALIZATION_QUEUE, app

MOTIVO_GRAVE_PADRAO = "conferência grave"
FUSO_DO_BRASIL = timezone(timedelta(hours=-3))
CONFERENCIAS_GRAVES = frozenset(
    c.nome for c in conferir(Bilhete(), origem=Origem.IA).conferencias if c.forca in GRAVES
)
PREFIXOS_DA_METRICA = (
    ("a foto tem ", "cupons sem stake"),
    ("a leitura falhou", "leitura falhou"),
    (MOTIVO_GRAVE_PADRAO, MOTIVO_GRAVE_PADRAO),
)
MOTIVO_SEM_CATEGORIA = "outro"


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
    revisao: str | None
    revisao_grave: bool


def motivo_da_metrica(motivo: str) -> str:
    # O motivo é texto livre e junta, na ordem do `conferir`, conferências que não abrem revisão
    # sozinhas. O rótulo é a primeira grave (a extração confere como IA), para as séries do
    # Prometheus terem limite e o painel não culpar uma conferência que só avisou.
    partes = re.split(r"; | · ", motivo)
    for parte in partes:
        nome = parte.split(": ", 1)[0]
        if nome in CONFERENCIAS_GRAVES:
            return nome
    for prefixo, rotulo in PREFIXOS_DA_METRICA:
        if any(parte.startswith(prefixo) for parte in partes):
            return rotulo
    return MOTIVO_SEM_CATEGORIA


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


async def _historico(
    session: AsyncSession, usuario_id: int, chave: str
) -> list[tuple[str, str, dict[str, Any]]]:
    # Entrega "pelo menos uma vez": duas cópias da mesma aposta não podem decidir juntas que
    # ela é nova.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"{usuario_id}:{chave}"},
    )
    return [
        (e.tipo, e.fonte, e.payload_json)
        for e in await EventoRepo().list_by_aposta_chave(session, usuario_id, chave)
    ]


def _financeira(estado: dict[str, Any]) -> ApostaFinanceira:
    return ApostaFinanceira(
        stake_unidades=float(estado.get("stake_unidades") or 0.0),
        valor_unidade_centavos=int(
            estado.get("valor_unidade_centavos") or VALOR_UNIDADE_PADRAO_CENTAVOS
        ),
        freebet=bool(estado.get("freebet")),
    )


async def _gravar(
    session: AsyncSession,
    usuario_id: int,
    nova: NovaAposta,
    extracao_json: dict[str, Any],
    midia_hash: str | None,
) -> Gravada:
    await _set_current_user(session, usuario_id)
    historico = await _historico(session, usuario_id, nova.chave)
    atual, protegidos = projetar(historico)
    criada = not any(tipo == "APOSTA_CRIADA" for tipo, _, _ in historico)
    novos = (
        [EventoNovo("APOSTA_CRIADA", "ia", nova.payload, nova.confianca)]
        if criada
        else eventos_da_releitura(nova, atual, protegidos)
    )
    eventos = EventoRepo()
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
    financeira = _financeira(estado)
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

    revisao = await _revisar(
        session,
        usuario_id,
        nova.chave,
        estado,
        novos,
        criada,
        midia_hash,
        {"aposta_chave": nova.chave, "extracao": extracao_json},
    )
    return Gravada(
        nova.chave, nova.origem, criada, len(novos), revisao, bool(estado.get("revisao_grave"))
    )


async def _revisar(
    session: AsyncSession,
    usuario_id: int,
    chave: str,
    estado: dict[str, Any],
    novos: list[EventoNovo],
    criada: bool,
    midia_hash: str | None,
    extracao_bruta: dict[str, Any],
) -> str | None:
    grave = bool(estado.get("revisao_grave"))
    motivo = (estado.get("revisao_motivo") or MOTIVO_GRAVE_PADRAO) if grave else None
    motivo_mudou = any(e.tipo == "CORRECAO_MANUAL" and "revisao_motivo" in e.payload for e in novos)
    revisoes = RevisaoPendenteRepo()
    if motivo_mudou:
        # Como no projeto antigo, a fila segue a leitura: motivo novo substitui o velho, e a aposta
        # que ficou limpa sai da fila.
        await revisoes.resolve_superseded(session, usuario_id, chave, motivo)
    if (
        motivo is None
        or not (criada or motivo_mudou)
        or await revisoes.has_open(session, usuario_id, chave, motivo)
    ):
        return None
    await revisoes.create(
        session,
        {
            "usuario_id": usuario_id,
            "midia_hash": midia_hash,
            "motivo": motivo,
            "extracao_bruta": extracao_bruta,
        },
    )
    return motivo


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
            if gravada.revisao is not None:
                revisao_pendente_created.labels(reason=motivo_da_metrica(gravada.revisao)).inc()
            if gravada.criada:
                get_event_bus().publish(
                    ApostaCriada(usuario_id, gravada.chave, gravada.origem, gravada.revisao_grave)
                )
    return gravadas


def materializar_aposta(
    usuario_id: int, extracao_json: dict[str, Any], midia_hash: str | None = None
) -> dict[str, object]:
    with observe_stage(MATERIALIZATION_QUEUE), materialization_duration.time():
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
        batch_bets_processed.labels(stage=MATERIALIZATION_QUEUE).inc(len(gravadas))
    return {
        "usuario_id": usuario_id,
        "apostas": [g.chave for g in gravadas],
        "criadas": sum(g.criada for g in gravadas),
        "eventos": sum(g.eventos for g in gravadas),
        "revisoes": sum(g.revisao is not None for g in gravadas),
    }


materializar_aposta_task = app.task(
    name="materialization.materializar_aposta",
    autoretry_for=RETRY_ON,
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=False,
    max_retries=3,
)(materializar_aposta)


async def _gravar_coletada(
    session: AsyncSession, usuario_id: int, coleta: ColetaCasa, casa: str, nome_da_casa: str
) -> Gravada:
    coletada = replace(LEITORES[casa](coleta.bruto_json), casa=casa)
    chave = chave_casa(casa, coletada.identidade)
    historico = await _historico(session, usuario_id, chave)
    atual, _ = projetar(historico)
    criada = not any(tipo == "APOSTA_CRIADA" for tipo, _, _ in historico)
    data_aposta = _data(coletada.data_aposta)
    if criada:
        # A unidade vigente é a do dia que conta para a aposta da casa, o do jogo.
        unidade = (
            None
            if data_aposta is None
            else await UnidadeRepo().get_vigente(session, usuario_id, data_aposta)
        )
        valor_unidade = VALOR_UNIDADE_PADRAO_CENTAVOS if unidade is None else unidade.valor_centavos
        validar(coletada, valor_unidade)
        novos = eventos_da_criacao(coletada, valor_unidade)
    else:
        novos = eventos_do_resultado(coletada, atual)
    eventos = EventoRepo()
    for evento in novos:
        await eventos.append(
            session,
            {
                "usuario_id": usuario_id,
                "tipo": evento.tipo,
                "fonte": evento.fonte,
                "payload_json": evento.payload,
                "confianca": evento.confianca,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": chave,
            },
        )

    estado, _ = projetar([*historico, *((e.tipo, e.fonte, e.payload) for e in novos)])
    financeira = _financeira(estado)
    dados: dict[str, object] = {
        "usuario_id": usuario_id,
        "chave": chave,
        "origem": "casa",
        "data_aposta": _data(estado.get("data_aposta")),
        "stake_unidades": financeira.stake_unidades,
        "stake_centavos": financeira.stake_centavos,
        "valor_aposta_centavos": financeira.valor_aposta_centavos,
        "odd": estado.get("odd"),
        "freebet": financeira.freebet,
        "estado": estado.get("estado", "PENDENTE"),
        "retorno_centavos": estado.get("retorno_centavos"),
        "revisao_grave": bool(estado.get("revisao_grave")),
    }
    if criada:
        conta = await ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, usuario_id, nome_da_casa, data_aposta
        )
        dados["conta_casa_id"] = None if conta is None else conta.id
    if await ApostaRepo().upsert_materializada(session, dados) is None:
        raise GravacaoConcorrenteError(f"outra gravação mais nova de {chave} chegou antes")

    revisao = await _revisar(
        session,
        usuario_id,
        chave,
        estado,
        novos,
        criada,
        None,
        {"aposta_chave": chave, "coleta_id": coleta.id},
    )
    return Gravada(chave, "casa", criada, len(novos), revisao, bool(estado.get("revisao_grave")))


async def gravar_coleta(engine: AsyncEngine, usuario_id: int, coleta_id: int) -> Gravada | None:
    async with (
        engine.connect() as conexao,
        AsyncSession(bind=conexao, expire_on_commit=False) as session,
    ):
        async with session.begin():
            await _set_current_user(session, usuario_id)
            coletas = ColetaCasaRepo()
            # A linha é travada antes de ser lida: uma tarefa atrasada processa a captura mais nova,
            # nunca a velha por cima dela, e a rota só grava outra captura depois deste commit.
            coleta = await coletas.get_by_id_for_update(session, usuario_id, coleta_id)
            if coleta is None:
                return None
            nome_da_casa = await CasaRepo().get_nome_by_id(session, coleta.casa_id)
            casa = None if nome_da_casa is None else casa_da_coleta(nome_da_casa)
            if nome_da_casa is None or casa is None:
                return None
            gravada = await _gravar_coletada(session, usuario_id, coleta, casa, nome_da_casa)
            await coletas.set_processado(session, usuario_id, coleta.id)
        if gravada.revisao is not None:
            revisao_pendente_created.labels(reason=motivo_da_metrica(gravada.revisao)).inc()
        if gravada.criada:
            get_event_bus().publish(
                ApostaCriada(usuario_id, gravada.chave, "casa", gravada.revisao_grave)
            )
    return gravada


def materializar_coleta(usuario_id: int, coleta_id: int) -> dict[str, object]:
    with observe_stage(MATERIALIZATION_QUEUE):
        gravada = asyncio.run(gravar_coleta(get_engine(), usuario_id, coleta_id))
        if gravada is not None:
            batch_bets_processed.labels(stage=MATERIALIZATION_QUEUE).inc()
    return {
        "usuario_id": usuario_id,
        "coleta_id": coleta_id,
        "aposta": None if gravada is None else gravada.chave,
        "criada": gravada is not None and gravada.criada,
        "eventos": 0 if gravada is None else gravada.eventos,
        "revisao": gravada is not None and gravada.revisao is not None,
    }


materializar_coleta_task = app.task(
    name="materialization.materializar_coleta",
    autoretry_for=RETRY_ON,
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=False,
    max_retries=3,
)(materializar_coleta)
