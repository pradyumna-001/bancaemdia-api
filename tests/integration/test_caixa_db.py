from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bancaemdia import models
from bancaemdia.api.v1 import caixa
from bancaemdia.core.context import current_user_id
from bancaemdia.db import session as db_session
from bancaemdia.domain.caixa_service import BIGINT_MIN, saldo_da_conta
from bancaemdia.domain.registros import ContaCasa, Usuario
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]


def _json(resposta: JSONResponse) -> dict[str, object]:
    corpo: dict[str, object] = json.loads(resposta.body)
    return corpo


async def _usuario(session: AsyncSession, usuario_id: int) -> Usuario:
    usuario = await UsuarioRepo().get_by_id(session, usuario_id)
    assert usuario is not None
    return usuario


async def _criar_contas(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    usuario_id: int,
    quantidade: int = 2,
) -> list[ContaCasa]:
    sufixo = uuid4().hex[:10]
    async with engine_admin.begin() as conn:
        casas = []
        for indice in range(quantidade):
            casa_id = await conn.scalar(
                insert(models.Casa)
                .values(nome=f"Caixa {sufixo} {indice}")
                .returning(models.Casa.id)
            )
            assert casa_id is not None
            casas.append(int(casa_id))

    async with como(engine_app, usuario_id) as session:
        contas = [
            await ContaCasaRepo().create(
                session,
                {"usuario_id": usuario_id, "casa_id": casa_id, "apelido": str(indice)},
            )
            for indice, casa_id in enumerate(casas)
        ]
        await session.commit()
        return contas


async def _postar(
    engine_app: AsyncEngine,
    como: Como,
    usuario_id: int,
    chave_idempotencia: str | None = None,
    **pedido: object,
) -> JSONResponse:
    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        return await caixa.registrar_movimento(
            caixa.MovimentoNovo.model_validate(pedido),
            usuario,
            session,
            chave_idempotencia or f"teste:{uuid4()}",
        )


async def test_retry_idempotente_deposito_retorna_a_mesma_resposta_e_uma_linha(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    (conta,) = await _criar_contas(engine_admin, engine_app, como, usuario_id, 1)
    chave = f"deposito:{uuid4()}"
    pedido = {
        "tipo": "deposito",
        "valor_centavos": 100_000,
        "conta_casa_id": conta.id,
        "ocorrido_em": datetime(2026, 9, 20, 12, tzinfo=UTC),
    }

    primeira = await _postar(
        engine_app,
        como,
        usuario_id,
        chave_idempotencia=chave,
        **pedido,
    )
    repetida = await _postar(
        engine_app,
        como,
        usuario_id,
        chave_idempotencia=chave,
        **pedido,
    )

    assert primeira.status_code == repetida.status_code == 201
    assert _json(primeira) == _json(repetida)
    async with como(engine_app, usuario_id) as session:
        assert await session.scalar(select(func.count()).select_from(models.Movimento)) == 1
        assert await session.scalar(select(func.count()).select_from(models.Evento)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.MovimentoRequisicao)) == 1
        )


async def test_retries_concorrentes_esperam_e_reapresentam_a_resposta_original(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usuario_id = await novo_usuario()
    (conta,) = await _criar_contas(engine_admin, engine_app, como, usuario_id, 1)
    chave = f"deposito-concorrente:{uuid4()}"
    pedido = {
        "tipo": "deposito",
        "valor_centavos": 100_000,
        "conta_casa_id": conta.id,
        "ocorrido_em": datetime(2026, 9, 20, 12, tzinfo=UTC),
    }
    primeira_no_evento = asyncio.Event()
    segunda_na_trava = asyncio.Event()
    liberar_primeira = asyncio.Event()
    chamadas_da_trava = 0
    append_evento_real = caixa.EventoRepo.append
    travar_real = caixa._travar_idempotencia

    async def observar_trava(
        session: AsyncSession,
        usuario_lido: int,
        chave_lida: str,
    ) -> None:
        nonlocal chamadas_da_trava
        chamadas_da_trava += 1
        if chamadas_da_trava == 2:
            segunda_na_trava.set()
        await travar_real(session, usuario_lido, chave_lida)

    async def pausar_primeiro_evento(
        self: EventoRepo,
        session: AsyncSession,
        dados: dict[str, object],
    ) -> object:
        primeira_no_evento.set()
        await liberar_primeira.wait()
        return await append_evento_real(self, session, dados)

    monkeypatch.setattr(caixa, "_travar_idempotencia", observar_trava)
    monkeypatch.setattr(caixa.EventoRepo, "append", pausar_primeiro_evento)
    primeira_tarefa = asyncio.create_task(
        _postar(
            engine_app,
            como,
            usuario_id,
            chave_idempotencia=chave,
            **pedido,
        )
    )
    segunda_tarefa: asyncio.Task[JSONResponse] | None = None
    try:
        await asyncio.wait_for(primeira_no_evento.wait(), timeout=5)
        segunda_tarefa = asyncio.create_task(
            _postar(
                engine_app,
                como,
                usuario_id,
                chave_idempotencia=chave,
                **pedido,
            )
        )
        await asyncio.wait_for(segunda_na_trava.wait(), timeout=5)
        concluidas, _ = await asyncio.wait({segunda_tarefa}, timeout=0.05)
        assert not concluidas
    finally:
        liberar_primeira.set()

    assert segunda_tarefa is not None
    primeira, repetida = await asyncio.wait_for(
        asyncio.gather(primeira_tarefa, segunda_tarefa),
        timeout=5,
    )
    assert primeira.status_code == repetida.status_code == 201
    assert _json(primeira) == _json(repetida)
    async with como(engine_app, usuario_id) as session:
        assert await session.scalar(select(func.count()).select_from(models.Movimento)) == 1
        assert await session.scalar(select(func.count()).select_from(models.Evento)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.MovimentoRequisicao)) == 1
        )


async def test_retry_idempotente_transferencia_nao_duplica_o_par_e_conflito_e_isolado(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    primeiro_usuario, segundo_usuario = await novo_usuario(), await novo_usuario()
    origem, destino = await _criar_contas(
        engine_admin,
        engine_app,
        como,
        primeiro_usuario,
    )
    (conta_segundo,) = await _criar_contas(
        engine_admin,
        engine_app,
        como,
        segundo_usuario,
        1,
    )
    chave = f"transferencia:{uuid4()}"
    pedido = {
        "tipo": "transferencia",
        "valor_centavos": 20_000,
        "conta_casa_id": origem.id,
        "conta_casa_destino_id": destino.id,
        "ocorrido_em": datetime(2026, 9, 20, 12, tzinfo=UTC),
    }

    primeira = await _postar(
        engine_app,
        como,
        primeiro_usuario,
        chave_idempotencia=chave,
        **pedido,
    )
    repetida = await _postar(
        engine_app,
        como,
        primeiro_usuario,
        chave_idempotencia=chave,
        **pedido,
    )
    conflito = await _postar(
        engine_app,
        como,
        primeiro_usuario,
        chave_idempotencia=chave,
        **{**pedido, "valor_centavos": 21_000},
    )
    outro_usuario = await _postar(
        engine_app,
        como,
        segundo_usuario,
        chave_idempotencia=chave,
        tipo="deposito",
        valor_centavos=5_000,
        conta_casa_id=conta_segundo.id,
        ocorrido_em=datetime(2026, 9, 20, 12, tzinfo=UTC),
    )

    assert _json(primeira) == _json(repetida)
    assert conflito.status_code == 409
    assert outro_usuario.status_code == 201
    async with como(engine_app, primeiro_usuario) as session:
        assert await session.scalar(select(func.count()).select_from(models.Movimento)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(models.MovimentoRequisicao)) == 1
        )
    async with como(engine_app, segundo_usuario) as session:
        assert await session.scalar(select(func.count()).select_from(models.Movimento)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.MovimentoRequisicao)) == 1
        )


async def test_transferencia_grava_o_par_e_um_evento_na_mesma_transacao(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    origem, destino = await _criar_contas(engine_admin, engine_app, como, usuario_id)
    quando = datetime(2026, 9, 20, 12, tzinfo=UTC)

    deposito = await _postar(
        engine_app,
        como,
        usuario_id,
        tipo="deposito",
        valor_centavos=100_000,
        conta_casa_id=origem.id,
        ocorrido_em=quando,
    )
    transferencia = await _postar(
        engine_app,
        como,
        usuario_id,
        tipo="transferencia",
        valor_centavos=20_000,
        conta_casa_id=origem.id,
        conta_casa_destino_id=destino.id,
        ocorrido_em=quando,
    )

    assert deposito.status_code == 201
    assert transferencia.status_code == 201
    transferencia_id = _json(transferencia)["transferencia_id"]
    assert transferencia_id

    async with como(engine_app, usuario_id) as session:
        movimentos = await MovimentoRepo().list_by_usuario(session, usuario_id)
        eventos = await EventoRepo().list_by_usuario(session, usuario_id)

    par = [
        movimento for movimento in movimentos if str(movimento.transferencia_id) == transferencia_id
    ]
    assert [(movimento.conta_casa_id, movimento.valor_centavos) for movimento in par] == [
        (origem.id, -20_000),
        (destino.id, 20_000),
    ]
    assert {movimento.ocorrido_em for movimento in par} == {quando}
    assert [evento.tipo for evento in eventos] == [
        "MOVIMENTO_REGISTRADO",
        "MOVIMENTO_REGISTRADO",
    ]
    assert len(eventos[0].payload_json["lancamentos"]) == 2


async def test_falha_no_evento_desfaz_os_dois_lados_da_transferencia(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usuario_id = await novo_usuario()
    origem, destino = await _criar_contas(engine_admin, engine_app, como, usuario_id)

    async def falhar(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("evento sabotado")

    monkeypatch.setattr(caixa.EventoRepo, "append", falhar)
    with pytest.raises(RuntimeError, match="sabotado"):
        await _postar(
            engine_app,
            como,
            usuario_id,
            tipo="transferencia",
            valor_centavos=20_000,
            conta_casa_id=origem.id,
            conta_casa_destino_id=destino.id,
            ocorrido_em=datetime(2026, 9, 20, 12, tzinfo=UTC),
        )

    async with como(engine_app, usuario_id) as session:
        movimentos = await session.scalar(select(func.count()).select_from(models.Movimento))
        eventos = await session.scalar(select(func.count()).select_from(models.Evento))
    assert (movimentos, eventos) == (0, 0)


async def test_conta_alheia_fica_invisivel_e_trava_ocupada_responde_rapido(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    dono, outro = await novo_usuario(), await novo_usuario()
    (conta,) = await _criar_contas(engine_admin, engine_app, como, dono, 1)
    pedido = {
        "tipo": "saque",
        "valor_centavos": 10_000,
        "conta_casa_id": conta.id,
        "ocorrido_em": datetime(2026, 9, 20, 12, tzinfo=UTC),
    }

    alheia = await _postar(engine_app, como, outro, **pedido)
    assert alheia.status_code == 404

    async with como(engine_app, dono) as segurando:
        chave = f"caixa:{dono}:{conta.id}"
        await segurando.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
            {"chave": chave},
        )
        ocupada = await _postar(engine_app, como, dono, **pedido)
        assert ocupada.status_code == 409
        await segurando.rollback()

    async with como(engine_app, dono) as session:
        assert await session.scalar(select(func.count()).select_from(models.Movimento)) == 0


async def test_saldo_temporal_nao_conta_o_green_duas_vezes_e_soma_banca_vinculada(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    (conta,) = await _criar_contas(engine_admin, engine_app, como, usuario_id, 1)
    await _postar(
        engine_app,
        como,
        usuario_id,
        tipo="deposito",
        valor_centavos=100_000,
        conta_casa_id=conta.id,
        ocorrido_em=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    async with como(engine_app, usuario_id) as session:
        await ApostaRepo().upsert_idempotent(
            session,
            {
                "usuario_id": usuario_id,
                "chave": f"m:{uuid4().hex}",
                "conta_casa_id": conta.id,
                "origem": "manual",
                "stake_unidades": 1.0,
                "stake_centavos": 10_000,
                "valor_aposta_centavos": 10_000,
                "odd": 2.0,
                "estado": "GREEN",
                "retorno_centavos": 20_000,
                "data_aposta": datetime(2026, 7, 2, 12, tzinfo=UTC),
                "selecionada": True,
            },
        )
        banca_id = await session.scalar(
            insert(models.Banca)
            .values(
                usuario_id=usuario_id,
                nome="Principal",
                saldo_inicial_centavos=50_000,
            )
            .returning(models.Banca.id)
        )
        assert banca_id is not None
        await session.commit()

    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        atual = _json(await caixa.consultar_saldo(usuario, session, None))
    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        antes = _json(await caixa.consultar_saldo(usuario, session, date(2026, 7, 1)))

    conta_atual = atual["contas"][0]
    assert conta_atual["saldo_atual_centavos"] == 110_000
    assert atual["saldo_total_centavos"] == 110_000
    assert antes["contas"][0]["saldo_atual_centavos"] == 100_000
    assert atual["bancas"][0]["saldo_inicial_centavos"] == 50_000
    assert atual["bancas"][0]["saldo_total_centavos"] is None
    assert "vínculo" in atual["bancas"][0]["motivo_saldo_indisponivel"]

    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        resposta = await caixa.vincular_banca(
            conta.id, caixa.VinculoBancaNovo(banca_id=banca_id), usuario, session
        )
        assert _json(resposta) == {"conta_casa_id": conta.id, "banca_id": banca_id}
    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        vinculada = _json(await caixa.consultar_saldo(usuario, session, None))
    assert vinculada["contas"][0]["banca_id"] == banca_id
    assert vinculada["bancas"][0]["saldo_total_centavos"] == 110_000
    assert vinculada["bancas"][0]["motivo_saldo_indisponivel"] is None


async def test_agregado_sql_equivale_ao_dominio_em_casos_financeiros_e_de_fronteira(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    principal, sem_movimentos, limite_bigint = await _criar_contas(
        engine_admin,
        engine_app,
        como,
        usuario_id,
        3,
    )
    corte = date(2026, 9, 21)

    for valor, ocorrido_em in (
        (100_000, datetime(2026, 9, 21, 12, tzinfo=UTC)),
        (20_000, datetime(2026, 9, 22, 2, 59, 59, 999999, tzinfo=UTC)),
        (500_000, datetime(2026, 9, 22, 3, tzinfo=UTC)),
    ):
        resposta = await _postar(
            engine_app,
            como,
            usuario_id,
            tipo="deposito",
            valor_centavos=valor,
            conta_casa_id=principal.id,
            ocorrido_em=ocorrido_em,
        )
        assert resposta.status_code == 201

    async with como(engine_app, usuario_id) as session:
        apostas = (
            # Antes do primeiro movimento: afeta lucro histórico, mas não o saldo do período.
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 10_000,
                "valor_aposta_centavos": 10_000,
                "estado": "RED",
                "retorno_centavos": 0,
                "data_aposta": datetime(2026, 9, 20, 12, tzinfo=UTC),
            },
            # Freebet: a stake contábil é zero e apenas o retorno entra no caixa/lucro.
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 0,
                "valor_aposta_centavos": 10_000,
                "estado": "GREEN",
                "retorno_centavos": 8_000,
                "freebet": True,
                "data_aposta": datetime(2026, 9, 21, 15, tzinfo=UTC),
            },
            # Revisão grave movimenta o caixa, mas não entra no lucro canônico.
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 10_000,
                "valor_aposta_centavos": 10_000,
                "estado": "GREEN",
                "retorno_centavos": 20_000,
                "revisao_grave": True,
                "data_aposta": datetime(2026, 9, 21, 16, tzinfo=UTC),
            },
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 5_000,
                "valor_aposta_centavos": 5_000,
                "estado": "PENDENTE",
                "retorno_centavos": None,
                "data_aposta": datetime(2026, 9, 21, 17, tzinfo=UTC),
            },
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 4_000,
                "valor_aposta_centavos": 4_000,
                "estado": "ANULADA",
                "retorno_centavos": 4_000,
                "data_aposta": datetime(2026, 9, 21, 18, tzinfo=UTC),
            },
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 90_000,
                "valor_aposta_centavos": 90_000,
                "estado": "RED",
                "retorno_centavos": 0,
                "selecionada": False,
                "data_aposta": datetime(2026, 9, 21, 19, tzinfo=UTC),
            },
            # Sem data_aposta, o corte usa criada_em; ainda é o último microssegundo do dia no Brasil.
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 2_000,
                "valor_aposta_centavos": 2_000,
                "estado": "RED",
                "retorno_centavos": 0,
                "data_aposta": None,
                "criada_em": datetime(2026, 9, 22, 2, 59, 59, 999999, tzinfo=UTC),
            },
            # Exatamente 03:00 UTC já é 22/09 no Brasil e fica fora do corte de 21/09.
            {
                "conta_casa_id": principal.id,
                "stake_centavos": 7_000,
                "valor_aposta_centavos": 7_000,
                "estado": "GREEN",
                "retorno_centavos": 14_000,
                "data_aposta": datetime(2026, 9, 22, 3, tzinfo=UTC),
            },
            {
                "conta_casa_id": sem_movimentos.id,
                "stake_centavos": 3_000,
                "valor_aposta_centavos": 3_000,
                "estado": "RED",
                "retorno_centavos": 0,
                "data_aposta": datetime(2026, 9, 21, 15, tzinfo=UTC),
            },
        )
        for indice, dados in enumerate(apostas):
            await ApostaRepo().upsert_idempotent(
                session,
                {
                    "usuario_id": usuario_id,
                    "chave": f"diferencial:{uuid4()}:{indice}",
                    "origem": "manual",
                    "stake_unidades": 1.0,
                    "odd": 2.0,
                    "selecionada": True,
                    "freebet": False,
                    "revisao_grave": False,
                    **dados,
                },
            )
        await MovimentoRepo().append(
            session,
            {
                "usuario_id": usuario_id,
                "conta_casa_id": limite_bigint.id,
                "tipo": "SAQUE",
                "valor_centavos": BIGINT_MIN,
                "ocorrido_em": datetime(2026, 9, 21, 12, tzinfo=UTC),
                "descricao": "limite inferior válido do ledger",
                "transferencia_id": None,
            },
        )
        await session.commit()

    async with como(engine_app, usuario_id) as session:
        apostas_persistidas = await ApostaRepo().list_by_usuario(session, usuario_id)
        movimentos_persistidos = await MovimentoRepo().list_by_usuario(session, usuario_id)
        observado = await MovimentoRepo().aggregate_saldos_by_usuario(
            session,
            usuario_id,
            corte,
        )

    for conta in (principal, sem_movimentos, limite_bigint):
        esperado = saldo_da_conta(
            conta.id,
            apostas_persistidas,
            movimentos_persistidos,
            corte,
        )
        assert observado[conta.id] == esperado

    assert observado[principal.id].apostas_antes_do_caixa == 1
    assert observado[principal.id].lucro_centavos == -4_000
    assert observado[sem_movimentos.id].saldo_centavos is None
    assert observado[sem_movimentos.id].lucro_centavos == -3_000
    assert observado[limite_bigint.id].sacado_centavos == abs(BIGINT_MIN)


async def test_saldo_uses_one_snapshot_when_money_changes_between_its_reads(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usuario_id = await novo_usuario()
    (conta,) = await _criar_contas(engine_admin, engine_app, como, usuario_id, 1)
    quando = datetime(2026, 7, 1, 12, tzinfo=UTC)
    await _postar(
        engine_app,
        como,
        usuario_id,
        tipo="deposito",
        valor_centavos=100_000,
        conta_casa_id=conta.id,
        ocorrido_em=quando,
    )
    agregar_real = MovimentoRepo.aggregate_saldos_by_usuario

    async def agregar_e_intercalar(
        self: MovimentoRepo,
        session: AsyncSession,
        usuario_lido: int,
        data_corte: date | None = None,
    ):
        saldos = await agregar_real(self, session, usuario_lido, data_corte)
        await _postar(
            engine_app,
            como,
            usuario_id,
            tipo="deposito",
            valor_centavos=100_000,
            conta_casa_id=conta.id,
            ocorrido_em=quando,
        )
        async with como(engine_app, usuario_id) as concorrente:
            await ApostaRepo().upsert_idempotent(
                concorrente,
                {
                    "usuario_id": usuario_id,
                    "chave": f"m:{uuid4().hex}",
                    "conta_casa_id": conta.id,
                    "origem": "manual",
                    "stake_unidades": 1.0,
                    "stake_centavos": 200_000,
                    "valor_aposta_centavos": 200_000,
                    "odd": 2.0,
                    "estado": "RED",
                    "retorno_centavos": 0,
                    "data_aposta": datetime(2026, 7, 2, 12, tzinfo=UTC),
                    "selecionada": True,
                },
            )
            await concorrente.commit()
        return saldos

    snapshot = engine_app.execution_options(isolation_level="REPEATABLE READ")
    fabrica = async_sessionmaker(snapshot, expire_on_commit=False, class_=AsyncSession)
    token = current_user_id.set(str(usuario_id))
    gerador = db_session.get_db_snapshot()
    try:
        monkeypatch.setattr(db_session, "SnapshotSessionLocal", fabrica)
        session = await anext(gerador)
        usuario = await _usuario(session, usuario_id)
        with monkeypatch.context() as intercalacao:
            intercalacao.setattr(
                MovimentoRepo,
                "aggregate_saldos_by_usuario",
                agregar_e_intercalar,
            )
            durante = _json(await caixa.consultar_saldo(usuario, session, None))
    finally:
        await gerador.aclose()
        current_user_id.reset(token)

    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        depois = _json(await caixa.consultar_saldo(usuario, session, None))

    assert durante["contas"][0]["saldo_atual_centavos"] == 100_000
    assert depois["contas"][0]["saldo_atual_centavos"] == 0


async def test_filtros_temporais_e_extrato_consolidado_funcionam_no_postgresql(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    (conta,) = await _criar_contas(engine_admin, engine_app, como, usuario_id, 1)
    for dia in (1, 10, 20):
        await _postar(
            engine_app,
            como,
            usuario_id,
            tipo="deposito",
            valor_centavos=dia * 100,
            conta_casa_id=conta.id,
            ocorrido_em=datetime(2026, 7, dia, 12, tzinfo=UTC),
        )

    async with como(engine_app, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        pagina = _json(
            await caixa.listar_movimentos(
                usuario,
                session,
                conta_casa_id=conta.id,
                tipo="deposito",
                desde=datetime(2026, 7, 10, 12, tzinfo=UTC),
                ate=datetime(2026, 7, 20, 12, tzinfo=UTC),
                page=1,
                page_size=50,
            )
        )
        extrato = _json(
            await caixa.consultar_extrato(
                usuario,
                session,
                conta_casa_id=conta.id,
                desde=None,
                ate=None,
                page=1,
                page_size=50,
            )
        )

    assert pagina["pagination"]["total"] == 1
    assert pagina["data"][0]["valor_centavos"] == 1_000
    assert extrato["pagination"]["total"] == 3
    assert {item["origem"] for item in extrato["data"]} == {"movimento"}
