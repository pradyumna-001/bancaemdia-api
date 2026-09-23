from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.workers.pairing import confirmar_par, parear_criacao

pytestmark = pytest.mark.xdist_group("postgres")


async def _criar(
    engine: AsyncEngine, usuario: int, origem: str, chave: str, payload: dict[str, object]
) -> str:
    mensagem = uuid4().int & ((1 << 62) - 1)
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario,
                "tipo": "APOSTA_CRIADA",
                "fonte": "casa" if origem == "casa" else "ia",
                "payload_json": {
                    "origem": origem,
                    "stake_unidades": 1.0,
                    "valor_unidade_centavos": 10_000,
                    **payload,
                },
                "confianca": None,
                "chat_id": 1 if origem == "telegram" else None,
                "message_id": mensagem if origem == "telegram" else None,
                "aposta_chave": chave,
            },
        )
        aposta = await ApostaRepo().upsert_materializada(
            session,
            {
                "usuario_id": usuario,
                "chave": chave,
                "origem": origem,
                "chat_id": 1 if origem == "telegram" else None,
                "message_id": mensagem if origem == "telegram" else None,
                "data_aposta": datetime.fromisoformat(str(payload["data_aposta"])),
                "stake_unidades": 1.0,
                "stake_centavos": 10_000,
                "valor_aposta_centavos": 10_000,
                "odd": 2.0,
                "freebet": False,
                "estado": "PENDENTE",
                "revisao_grave": False,
                "selecionada": True,
            },
        )
        assert aposta is not None
        if origem != "casa":
            return await parear_criacao(session, usuario, aposta, payload)
    return "nova"


async def test_two_matching_tips_are_serialized_and_only_one_owns_the_house_bet(
    engine_app: AsyncEngine, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    usuario = await novo_usuario()
    payload = {
        "casa": "Betano",
        "evento": "Internacional - Corinthians",
        "data_aposta": "2026-09-20T19:30:00-03:00",
        "comeca_em": "2026-09-20T19:30:00-03:00",
        "mercado_bruto": "Total de gols",
        "descricao": "Mais de 2.5 gols",
    }
    casa = f"c:betano:{uuid4().hex}"
    dicas = [f"t:{uuid4().hex}" for _ in range(2)]
    await _criar(engine_app, usuario, "casa", casa, payload)
    resultados = await asyncio.wait_for(
        asyncio.gather(
            *(_criar(engine_app, usuario, "telegram", chave, payload) for chave in dicas)
        ),
        timeout=10,
    )
    assert sorted(resultados) == ["duvida", "igual"]
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        apostas = {
            aposta.chave: aposta
            for aposta in (
                await session.execute(
                    select(models.Aposta).where(models.Aposta.usuario_id == usuario)
                )
            ).scalars()
        }
    assert apostas[casa].selecionada is True
    assert apostas[casa].parceira_chave in dicas
    assert sum(apostas[chave].duvida_de_par for chave in dicas) == 1
    assert all(not apostas[chave].selecionada for chave in dicas)


async def test_manual_confirmation_of_an_uncertain_pair_keeps_the_house_bet(
    engine_app: AsyncEngine, novo_usuario: Callable[[], Awaitable[int]]
) -> None:
    usuario = await novo_usuario()
    payload = {
        "casa": "Betano",
        "evento": "Internacional - Corinthians",
        "data_aposta": "2026-09-20T19:30:00-03:00",
        "comeca_em": "2026-09-20T19:30:00-03:00",
        "mercado_bruto": "Total de gols",
        "descricao": "Mais de 2.5 gols",
    }
    casa, dica = f"c:betano:{uuid4().hex}", f"t:{uuid4().hex}"
    await _criar(engine_app, usuario, "casa", casa, payload)
    assert (
        await _criar(engine_app, usuario, "telegram", dica, {**payload, "descricao": "3+ gols"})
        == "duvida"
    )
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        aposta_casa = await ApostaRepo().get_by_chave_for_update(session, usuario, casa)
        aposta_dica = await ApostaRepo().get_by_chave_for_update(session, usuario, dica)
        assert aposta_casa is not None and aposta_dica is not None
        await confirmar_par(session, usuario, aposta_dica, aposta_casa)
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario)}
        )
        aposta_casa = await ApostaRepo().get_by_chave(session, usuario, casa)
        aposta_dica = await ApostaRepo().get_by_chave(session, usuario, dica)
    assert aposta_casa is not None and aposta_dica is not None
    assert aposta_casa.selecionada is True and aposta_casa.parceira_chave == dica
    assert aposta_dica.selecionada is False and aposta_dica.duvida_de_par is False
