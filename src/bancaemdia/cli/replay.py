"""Reconcile bet projections and restore event-backed cash rows safely."""

import argparse
import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as day_time
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.config import get_settings
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
)
from bancaemdia.domain.financeiro import Aposta as ApostaFinanceira
from bancaemdia.domain.projecao import EventoIrrecuperavelError, projetar_validado
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.models.movimento import TIPOS_DE_MOVIMENTO
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.workers.materialization import get_engine

FUSO = ZoneInfo("America/Sao_Paulo")
PAGE_SIZE = 1000
COLUNAS = (
    "origem",
    "chat_id",
    "message_id",
    "ordem_na_mensagem",
    "midia_hash",
    "conta_casa_id",
    "tipster_id",
    "time_casa_id",
    "time_fora_id",
    "mercado_id",
    "competicao_id",
    "data_aposta",
    "data_jogo",
    "stake_unidades",
    "stake_centavos",
    "valor_aposta_centavos",
    "odd",
    "retorno_centavos",
    "estado",
    "freebet",
    "revisao_grave",
    "selecionada",
    "duvida_de_par",
    "parceira_chave",
)
IDENTIFICADORES = (
    "conta_casa_id",
    "tipster_id",
    "time_casa_id",
    "time_fora_id",
    "mercado_id",
    "competicao_id",
)


class ReplayInseguroError(RuntimeError):
    pass


@dataclass
class ResultadoReplay:
    usuario_id: int
    eventos: int = 0
    apostas: int = 0
    alteradas: int = 0
    recriadas: int = 0
    movimentos: int = 0
    movimentos_sem_evento: int = 0
    movimentos_restaurados: int = 0
    seco: bool = False


def _data(valor: object) -> datetime | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        instante = valor
    elif isinstance(valor, str):
        try:
            instante = datetime.fromisoformat(valor)
        except ValueError as erro:
            raise ReplayInseguroError(f"data inválida no evento: {valor!r}") from erro
    else:
        raise ReplayInseguroError(f"data inválida no evento: {valor!r}")
    return instante if instante.tzinfo else instante.replace(tzinfo=FUSO)


def _limite(valor: date | datetime | None) -> datetime | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return _data(valor)
    return datetime.combine(valor, day_time.min, tzinfo=FUSO)


def _selecionada(instante: datetime, desde: datetime | None, ate: datetime | None) -> bool:
    return (desde is None or instante >= desde) and (ate is None or instante < ate)


async def _identidade(session: AsyncSession, usuario_id: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :usuario, true)"),
        {"usuario": str(usuario_id)},
    )


async def _preparar(session: AsyncSession, usuario_id: int, seco: bool) -> None:
    if await session.scalar(text("SELECT pg_is_in_recovery()")):
        raise ReplayInseguroError("replay exige conexão ao primary")
    await _identidade(session, usuario_id)
    if (
        await session.scalar(select(models.Usuario.id).where(models.Usuario.id == usuario_id))
        is None
    ):
        raise ReplayInseguroError(f"usuário {usuario_id} não existe")
    if not seco:
        # Each writer takes ROW EXCLUSIVE on its target table. NOWAIT refuses an in-flight writer;
        # these locks then block new writers until the comparison and updates commit atomically.
        await session.execute(
            text("LOCK TABLE eventos, apostas, movimentos IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        )


async def _pagina_chaves(
    session: AsyncSession, usuario_id: int, depois: str, tamanho: int
) -> list[str]:
    stmt = (
        select(models.Evento.aposta_chave)
        .where(models.Evento.usuario_id == usuario_id, models.Evento.aposta_chave > depois)
        .distinct()
        .order_by(models.Evento.aposta_chave)
        .limit(tamanho)
    )
    return [chave for chave in (await session.execute(stmt)).scalars() if chave is not None]


async def _valores(
    session: AsyncSession, usuario_id: int, estado: dict[str, Any], atual: Any
) -> dict[str, object]:
    valor_unidade = estado.get("valor_unidade_centavos")
    instante = _data(estado.get("data_aposta")) if "data_aposta" in estado else atual.data_aposta
    if valor_unidade is None:
        # Legacy creation events sometimes predate the unit snapshot.
        referencia = instante or atual.data_aposta or atual.criada_em
        unidade = await UnidadeRepo().get_vigente(session, usuario_id, referencia)
        valor_unidade = VALOR_UNIDADE_PADRAO_CENTAVOS if unidade is None else unidade.valor_centavos
    if not isinstance(valor_unidade, int) or valor_unidade <= 0:
        raise ReplayInseguroError("valor da unidade inválido no histórico")
    financeira = ApostaFinanceira(
        stake_unidades=float(estado.get("stake_unidades") or 0.0),
        valor_unidade_centavos=int(valor_unidade),
        freebet=bool(estado.get("freebet")),
    )
    if "valor_unidade_centavos" not in estado and (
        financeira.stake_centavos != atual.stake_centavos
        or financeira.valor_aposta_centavos != atual.valor_aposta_centavos
    ):
        raise ReplayInseguroError("unidade legada sem snapshot diverge da aposta gravada")
    dados: dict[str, object] = {
        "origem": estado["origem"],
        "stake_unidades": financeira.stake_unidades,
        "stake_centavos": financeira.stake_centavos,
        "valor_aposta_centavos": financeira.valor_aposta_centavos,
        "odd": estado.get("odd"),
        "freebet": financeira.freebet,
        "estado": estado.get("estado", "PENDENTE"),
        "retorno_centavos": estado.get("retorno_centavos"),
        "revisao_grave": bool(estado.get("revisao_grave")),
        "selecionada": bool(estado.get("selecionada", True)),
        "duvida_de_par": bool(estado.get("duvida_de_par")),
        "parceira_chave": estado.get("parceira_chave"),
        "data_aposta": instante,
        "data_jogo": _data(estado["data_jogo"]) if "data_jogo" in estado else atual.data_jogo,
    }
    for campo in (*IDENTIFICADORES, "chat_id", "message_id", "ordem_na_mensagem", "midia_hash"):
        if campo in estado:
            dados[campo] = estado[campo]
    if "conta_casa_id" not in estado:
        # Historic events omitted the account snapshot. Keep the original association;
        # a later account cannot claim a bet merely because its temporal interval overlaps.
        dados["conta_casa_id"] = atual.conta_casa_id
    conta_id = dados.get("conta_casa_id")
    if conta_id is not None and (
        not isinstance(conta_id, int)
        or await ContaCasaRepo().get_by_id(session, usuario_id, conta_id) is None
    ):
        raise ReplayInseguroError("conta histórica não pertence ao usuário")
    if conta_id is not None and estado.get("casa"):
        try:
            await attribute_account(session, usuario_id, estado["casa"], instante, conta_id)
        except InvalidAccountReferenceError as invalid:
            raise ReplayInseguroError("conta histórica não pertence a esta casa") from invalid
    return dados


async def _restaurar_ledger(
    session: AsyncSession, usuario_id: int, resultado: ResultadoReplay, seco: bool
) -> None:
    """Restore only rows whose immutable event records every material field and original ID."""
    depois = 0
    while True:
        eventos = (
            (
                await session.execute(
                    select(models.Evento)
                    .where(
                        models.Evento.usuario_id == usuario_id,
                        models.Evento.tipo == "MOVIMENTO_REGISTRADO",
                        models.Evento.id > depois,
                    )
                    .order_by(models.Evento.id)
                    .limit(PAGE_SIZE)
                )
            )
            .scalars()
            .all()
        )
        if not eventos:
            break
        for evento in eventos:
            depois = evento.id
            payload = evento.payload_json
            lancamentos = payload.get("lancamentos")
            instante = _data(payload.get("ocorrido_em"))
            if not isinstance(lancamentos, list) or instante is None:
                raise ReplayInseguroError(f"evento de caixa {evento.id} incompleto")
            transferencia = payload.get("transferencia_id")
            try:
                transferencia_id = None if transferencia is None else UUID(str(transferencia))
            except ValueError as erro:
                raise ReplayInseguroError(f"transferência inválida no evento {evento.id}") from erro
            for linha in lancamentos:
                if not isinstance(linha, dict) or not isinstance(linha.get("movimento_id"), int):
                    raise ReplayInseguroError(f"lançamento inválido no evento {evento.id}")
                movimento_id = linha["movimento_id"]
                existente = (
                    await session.execute(
                        select(models.Movimento).where(
                            models.Movimento.usuario_id == usuario_id,
                            models.Movimento.id == movimento_id,
                        )
                    )
                ).scalar_one_or_none()
                if existente is not None:
                    if existente.ocorrido_em != instante:
                        raise ReplayInseguroError(f"movimento {movimento_id} existe em outra data")
                    continue
                conta_id = linha.get("conta_casa_id")
                if conta_id is not None and (
                    not isinstance(conta_id, int)
                    or await ContaCasaRepo().get_by_id(session, usuario_id, conta_id) is None
                ):
                    raise ReplayInseguroError(f"conta inválida no evento {evento.id}")
                if linha.get("tipo") not in TIPOS_DE_MOVIMENTO or not isinstance(
                    linha.get("valor_centavos"), int
                ):
                    raise ReplayInseguroError(f"valor/tipo inválido no evento {evento.id}")
                resultado.movimentos_restaurados += 1
                if not seco:
                    await session.execute(
                        insert(models.Movimento).values(
                            id=movimento_id,
                            usuario_id=usuario_id,
                            conta_casa_id=conta_id,
                            tipo=linha["tipo"],
                            valor_centavos=linha["valor_centavos"],
                            ocorrido_em=instante,
                            descricao=payload.get("descricao"),
                            transferencia_id=transferencia_id,
                        )
                    )
        if len(eventos) < PAGE_SIZE:
            break


async def _conferir_ledger(
    session: AsyncSession, usuario_id: int, resultado: ResultadoReplay
) -> None:
    depois = 0
    while True:
        eventos = (
            (
                await session.execute(
                    select(models.Evento)
                    .where(
                        models.Evento.usuario_id == usuario_id,
                        models.Evento.tipo == "MOVIMENTO_REGISTRADO",
                        models.Evento.id > depois,
                    )
                    .order_by(models.Evento.id)
                    .limit(PAGE_SIZE)
                )
            )
            .scalars()
            .all()
        )
        if not eventos:
            break
        for evento in eventos:
            depois = evento.id
            payload = evento.payload_json
            lancamentos = payload.get("lancamentos")
            if not isinstance(lancamentos, list) or not lancamentos:
                raise ReplayInseguroError(f"evento de caixa {evento.id} sem lançamentos")
            if payload.get("tipo") == "TRANSFERENCIA" and len(lancamentos) != 2:
                raise ReplayInseguroError(f"transferência incompleta no evento {evento.id}")
            transferencia = payload.get("transferencia_id")
            if (payload.get("tipo") == "TRANSFERENCIA") != bool(transferencia):
                raise ReplayInseguroError(f"transferência sem par/ID no evento {evento.id}")
            soma = 0
            for linha in lancamentos:
                if not isinstance(linha, dict) or not isinstance(linha.get("movimento_id"), int):
                    raise ReplayInseguroError(f"lançamento inválido no evento {evento.id}")
                movimento_id = linha["movimento_id"]
                movimento = (
                    await session.execute(
                        select(models.Movimento).where(
                            models.Movimento.usuario_id == usuario_id,
                            models.Movimento.id == movimento_id,
                        )
                    )
                ).scalar_one_or_none()
                if movimento is None:
                    raise ReplayInseguroError(f"movimento {movimento_id} ausente")
                if any(
                    getattr(movimento, campo) != linha.get(campo)
                    for campo in ("tipo", "valor_centavos", "conta_casa_id")
                ) or str(movimento.transferencia_id or "") != str(transferencia or ""):
                    raise ReplayInseguroError(f"movimento {movimento_id} diverge do evento")
                if transferencia and movimento.tipo != "TRANSFERENCIA":
                    raise ReplayInseguroError(f"movimento {movimento_id} não é transferência")
                if movimento.ocorrido_em != _data(payload.get("ocorrido_em")):
                    raise ReplayInseguroError(f"data do movimento {movimento_id} diverge")
                soma += movimento.valor_centavos
            if transferencia and soma != 0:
                raise ReplayInseguroError(f"transferência {transferencia} não fecha")
        if len(eventos) < PAGE_SIZE:
            break
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(models.Movimento)
            .where(models.Movimento.usuario_id == usuario_id)
        )
        or 0
    )
    resultado.movimentos = total
    cobertos, distintos = (
        await session.execute(
            text(
                "SELECT count(*), count(DISTINCT (item->>'movimento_id')::bigint) "
                "FROM eventos e CROSS JOIN LATERAL "
                "jsonb_array_elements(e.payload_json->'lancamentos') item "
                "WHERE e.usuario_id = :uid AND e.tipo = 'MOVIMENTO_REGISTRADO'"
            ),
            {"uid": usuario_id},
        )
    ).one()
    if cobertos != distintos or distintos > total:
        raise ReplayInseguroError("IDs de movimentos duplicados no histórico")
    resultado.movimentos_sem_evento = total - distintos
    # Legacy movements are retained; they cannot be rebuilt from incomplete history.
    depois_requisicao = 0
    while True:
        requisicoes = (
            (
                await session.execute(
                    select(models.MovimentoRequisicao)
                    .where(
                        models.MovimentoRequisicao.usuario_id == usuario_id,
                        models.MovimentoRequisicao.id > depois_requisicao,
                    )
                    .order_by(models.MovimentoRequisicao.id)
                    .limit(PAGE_SIZE)
                )
            )
            .scalars()
            .all()
        )
        if not requisicoes:
            break
        for requisicao in requisicoes:
            depois_requisicao = requisicao.id
            linhas_resposta = requisicao.resposta_json.get("movimentos", [])
            if not isinstance(linhas_resposta, list):
                raise ReplayInseguroError("resposta idempotente inválida")
            for linha in linhas_resposta:
                if not isinstance(linha, dict) or not isinstance(linha.get("id"), int):
                    raise ReplayInseguroError("resposta idempotente inválida")
                movimento = (
                    await session.execute(
                        select(models.Movimento).where(
                            models.Movimento.usuario_id == usuario_id,
                            models.Movimento.id == linha["id"],
                        )
                    )
                ).scalar_one_or_none()
                evento_id = await session.scalar(
                    select(models.Evento.id)
                    .where(
                        models.Evento.usuario_id == usuario_id,
                        models.Evento.tipo == "MOVIMENTO_REGISTRADO",
                        models.Evento.payload_json["lancamentos"].contains([
                            {"movimento_id": linha["id"]}
                        ]),
                    )
                    .limit(1)
                )
                if movimento is None or evento_id is None:
                    raise ReplayInseguroError(
                        f"resposta idempotente aponta para {linha['id']} sem evento"
                    )
                if any(
                    linha.get(campo) != getattr(movimento, campo)
                    for campo in ("tipo", "valor_centavos", "conta_casa_id")
                ):
                    raise ReplayInseguroError(f"resposta idempotente de {linha['id']} diverge")
                if _data(linha.get("ocorrido_em")) != movimento.ocorrido_em or str(
                    movimento.transferencia_id or ""
                ) != str(linha.get("transferencia_id") or ""):
                    raise ReplayInseguroError(
                        f"data/transferência da resposta {linha['id']} diverge"
                    )
        if len(requisicoes) < PAGE_SIZE:
            break


async def reconstruir_usuario(
    usuario_id: int,
    desde: date | datetime | None = None,
    ate: date | datetime | None = None,
    *,
    engine: AsyncEngine | None = None,
    dry_run: bool = False,
    chunk_size: int = PAGE_SIZE,
) -> ResultadoReplay:
    """Reconcile proven derived columns, retaining row IDs, audit history and ledger.

    ``desde`` is inclusive and ``ate`` exclusive in the business time of a bet;
    every selected key is folded from its entire history, regardless of event time.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size deve ser positivo")
    inicio, fim = _limite(desde), _limite(ate)
    if inicio is not None and fim is not None and inicio >= fim:
        raise ValueError("desde deve ser anterior a ate")
    resultado = ResultadoReplay(usuario_id, seco=dry_run)
    relogio = time.monotonic()
    async with AsyncSession(engine or get_engine()) as session, session.begin():
        if dry_run:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await _preparar(session, usuario_id, dry_run)
        sem_chave = await session.scalar(
            select(models.Evento.id)
            .where(
                models.Evento.usuario_id == usuario_id,
                models.Evento.aposta_chave.is_(None),
                models.Evento.tipo != "MOVIMENTO_REGISTRADO",
            )
            .limit(1)
        )
        if sem_chave is not None:
            raise ReplayInseguroError(f"evento de aposta {sem_chave} sem chave")
        total_eventos = int(
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(
                    models.Evento.usuario_id == usuario_id,
                    models.Evento.aposta_chave.is_not(None),
                )
            )
            or 0
        )
        depois = ""
        # The transaction and table lock provide a fixed event set for a mutating replay.
        while True:
            chaves = await _pagina_chaves(session, usuario_id, depois, chunk_size)
            if not chaves:
                break
            eventos = (
                (
                    await session.execute(
                        select(models.Evento)
                        .where(
                            models.Evento.usuario_id == usuario_id,
                            models.Evento.aposta_chave.in_(chaves),
                        )
                        .order_by(models.Evento.aposta_chave, models.Evento.id)
                    )
                )
                .scalars()
                .all()
            )
            apostas = {
                aposta.chave: aposta
                for aposta in (
                    await session.execute(
                        select(models.Aposta).where(
                            models.Aposta.usuario_id == usuario_id,
                            models.Aposta.chave.in_(chaves),
                        )
                    )
                ).scalars()
            }
            por_chave: dict[str, list[models.Evento]] = {chave: [] for chave in chaves}
            for evento in eventos:
                por_chave[str(evento.aposta_chave)].append(evento)
            for chave in chaves:
                historico = por_chave[chave]
                resultado.eventos += len(historico)
                aposta = apostas.get(chave)
                try:
                    estado, _ = projetar_validado(
                        (evento.tipo, evento.fonte, evento.payload_json) for evento in historico
                    )
                except (EventoIrrecuperavelError, ValueError, TypeError) as erro:
                    raise ReplayInseguroError(
                        f"histórico irrecuperável de {chave}: {erro}"
                    ) from erro
                referencia = (
                    _data(estado.get("data_aposta"))
                    or (None if aposta is None else aposta.data_aposta)
                    or historico[0].criado_em
                )
                if not _selecionada(referencia, inicio, fim):
                    continue
                resultado.apostas += 1
                if aposta is None:
                    snapshot = historico[0].payload_json.get("snapshot_replay")
                    if (
                        not isinstance(snapshot, dict)
                        or not snapshot.get("snapshot_completo")
                        or not all(
                            campo in snapshot
                            for campo in (
                                "origem",
                                "stake_unidades",
                                "valor_unidade_centavos",
                                "conta_casa_id",
                                "chat_id",
                                "message_id",
                                "ordem_na_mensagem",
                                "data_aposta",
                                "freebet",
                            )
                        )
                    ):
                        raise ReplayInseguroError(
                            f"aposta {chave} sem linha; ID histórico não recuperável"
                        )
                    estado = {**snapshot, **estado}
                    referencia_antiga = SimpleNamespace(
                        data_aposta=_data(snapshot["data_aposta"]),
                        criada_em=historico[0].criado_em,
                        data_jogo=_data(snapshot.get("data_jogo")),
                        conta_casa_id=snapshot["conta_casa_id"],
                        stake_centavos=0,
                        valor_aposta_centavos=0,
                    )
                    dados = await _valores(session, usuario_id, estado, referencia_antiga)
                    resultado.recriadas += 1
                    if not dry_run:
                        await session.execute(
                            insert(models.Aposta).values(
                                usuario_id=usuario_id,
                                chave=chave,
                                criada_em=historico[0].criado_em,
                                atualizada_em=historico[-1].criado_em,
                                **dados,
                            )
                        )
                    continue
                dados = await _valores(session, usuario_id, estado, aposta)
                diferencas = {
                    campo: valor
                    for campo, valor in dados.items()
                    if campo in COLUNAS and getattr(aposta, campo) != valor
                }
                if diferencas:
                    resultado.alteradas += 1
                    if not dry_run:
                        await session.execute(
                            update(models.Aposta)
                            .where(
                                models.Aposta.usuario_id == usuario_id,
                                models.Aposta.id == aposta.id,
                            )
                            .values(**diferencas, atualizada_em=aposta.atualizada_em)
                        )
                if resultado.eventos // 100 > (resultado.eventos - len(historico)) // 100:
                    decorrido = time.monotonic() - relogio
                    structlog.get_logger().info(
                        "replay_progresso",
                        usuario_id=usuario_id,
                        eventos=resultado.eventos,
                        total_eventos=total_eventos,
                        percentual=round(resultado.eventos * 100 / max(total_eventos, 1), 1),
                        apostas=resultado.apostas,
                        alteradas=resultado.alteradas,
                        segundos=round(decorrido, 2),
                        eta_segundos=round(
                            decorrido
                            * max(total_eventos - resultado.eventos, 0)
                            / resultado.eventos,
                            2,
                        ),
                    )
            depois = chaves[-1]
            if len(chaves) < chunk_size:
                break
        # A projected row without any history is not safely reconstructible.
        orfas = await session.scalar(
            select(models.Aposta.id)
            .where(
                models.Aposta.usuario_id == usuario_id,
                ~select(models.Evento.id)
                .where(
                    models.Evento.usuario_id == usuario_id,
                    models.Evento.aposta_chave == models.Aposta.chave,
                )
                .exists(),
            )
            .limit(1)
        )
        if orfas is not None:
            raise ReplayInseguroError(f"aposta {orfas} sem histórico")
        await _restaurar_ledger(session, usuario_id, resultado, dry_run)
        if not dry_run or resultado.movimentos_restaurados == 0:
            await _conferir_ledger(session, usuario_id, resultado)
        if dry_run:
            await session.rollback()
    return resultado


async def reconstruir_tudo(
    *, confirm: bool = False, dry_run: bool = False
) -> list[ResultadoReplay]:
    if get_settings().APP_ENV not in {"development", "testing"}:
        raise ReplayInseguroError("reconstruir_tudo só roda em development/testing")
    if not confirm:
        raise ReplayInseguroError("reconstruir_tudo exige --confirm")
    engine = get_engine()
    resultados: list[ResultadoReplay] = []
    depois = 0
    while True:
        async with AsyncSession(engine) as session:
            ids = (
                (
                    await session.execute(
                        select(models.Usuario.id)
                        .where(models.Usuario.id > depois)
                        .order_by(models.Usuario.id)
                        .limit(PAGE_SIZE)
                    )
                )
                .scalars()
                .all()
            )
        for usuario_id in ids:
            resultados.append(await reconstruir_usuario(usuario_id, engine=engine, dry_run=dry_run))
        if len(ids) < PAGE_SIZE:
            return resultados
        depois = ids[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    alvo = parser.add_mutually_exclusive_group(required=True)
    alvo.add_argument("--usuario-id", type=int)
    alvo.add_argument("--tudo", action="store_true")
    parser.add_argument("--desde", type=date.fromisoformat)
    parser.add_argument("--ate", type=date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if args.tudo and (args.desde or args.ate):
        parser.error("--tudo não aceita intervalo")
    try:
        if args.tudo:
            resultados = asyncio.run(reconstruir_tudo(confirm=args.confirm, dry_run=args.dry_run))
        else:
            fim = args.ate + timedelta(days=1) if args.ate else None
            resultados = [
                asyncio.run(
                    reconstruir_usuario(args.usuario_id, args.desde, fim, dry_run=args.dry_run)
                )
            ]
    except (ReplayInseguroError, ValueError) as erro:
        structlog.get_logger().error("replay_recusado", motivo=str(erro))
        return 1
    for resultado in resultados:
        structlog.get_logger().info("replay_concluido", **vars(resultado))
    return 0
