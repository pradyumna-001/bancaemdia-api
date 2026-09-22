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


async def test_saldo_temporal_nao_conta_o_green_duas_vezes_e_nao_inventa_banca(
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
        await session.execute(
            insert(models.Banca).values(
                usuario_id=usuario_id,
                nome="Principal",
                saldo_inicial_centavos=50_000,
            )
        )
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
