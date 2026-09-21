from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1 import revisao as rota
from bancaemdia.domain.registros import Usuario
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo, MidiaRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = pytest.mark.xdist_group("postgres")

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]
NovoUsuario = Callable[[], Awaitable[int]]


@dataclass(frozen=True)
class Cenario:
    revisao_id: int
    chave: str
    motivo: str
    midia_hash: str | None


def _json(resposta: JSONResponse) -> dict[str, Any]:
    corpo: dict[str, Any] = json.loads(resposta.body)
    return corpo


async def _usuario(session: AsyncSession, usuario_id: int) -> Usuario:
    usuario = await UsuarioRepo().get_by_id(session, usuario_id)
    assert usuario is not None
    return usuario


async def _criar_revisao(
    engine: AsyncEngine,
    como: Como,
    usuario_id: int,
    *,
    motivo: str,
    criado_em: datetime | None = None,
    com_aposta: bool = True,
    com_historico: bool = True,
    foto: bytes | None = None,
) -> Cenario:
    chave = f"m:{uuid4().hex}"
    midia_hash = None if foto is None else sha256(foto).hexdigest()
    async with como(engine, usuario_id) as session:
        if foto is not None and midia_hash is not None:
            await MidiaRepo().upsert_idempotent(session, midia_hash, "image/jpeg", len(foto))
            await MidiaArquivoRepo().upsert_idempotent(session, midia_hash, foto)

        if com_aposta and com_historico:
            await EventoRepo().append(
                session,
                {
                    "usuario_id": usuario_id,
                    "tipo": "APOSTA_CRIADA",
                    "fonte": "ia",
                    "payload_json": {
                        "origem": "manual",
                        "casa": "Betano",
                        "odd": 1.8,
                        "stake_unidades": 1.0,
                        "valor_unidade_centavos": 10_000,
                        "freebet": False,
                        "selecionada": True,
                        "data_aposta": "2026-09-01T12:00:00+00:00",
                        "revisao_motivo": motivo,
                        "revisao_grave": True,
                    },
                    "confianca": 0.4,
                    "chat_id": None,
                    "message_id": None,
                    "aposta_chave": chave,
                },
            )
        if com_aposta:
            aposta = await ApostaRepo().upsert_materializada(
                session,
                {
                    "usuario_id": usuario_id,
                    "chave": chave,
                    "origem": "manual",
                    "stake_unidades": 1.0,
                    "stake_centavos": 10_000,
                    "valor_aposta_centavos": 10_000,
                    "odd": 1.8,
                    "freebet": False,
                    "estado": "PENDENTE",
                    "retorno_centavos": None,
                    "revisao_grave": True,
                    "selecionada": True,
                    "data_aposta": datetime(2026, 9, 1, 12, tzinfo=UTC),
                    "midia_hash": midia_hash,
                },
            )
            assert aposta is not None

        revisao = await RevisaoPendenteRepo().create(
            session,
            {
                "usuario_id": usuario_id,
                "midia_hash": midia_hash,
                "motivo": motivo,
                "extracao_bruta": {"aposta_chave": chave},
            },
        )
        if criado_em is not None:
            await session.execute(
                update(models.RevisaoPendente)
                .where(models.RevisaoPendente.id == revisao.id)
                .values(criado_em=criado_em)
            )
        await session.commit()
    return Cenario(revisao.id, chave, motivo, midia_hash)


async def _resolver(
    engine: AsyncEngine,
    como: Como,
    usuario_id: int,
    revisao_id: int,
    acao: str,
    aposta_corrigida: dict[str, object] | None = None,
) -> JSONResponse:
    async with como(engine, usuario_id) as session:
        usuario = await _usuario(session, usuario_id)
        pedido = rota.ResolucaoNova.model_validate({
            "acao": acao,
            "aposta_corrigida": aposta_corrigida,
        })
        return await rota.resolver_revisao(revisao_id, pedido, usuario, session)


async def test_fila_filtros_paginacao_stats_e_rls_usam_as_mesmas_abertas(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    dono, outro = await novo_usuario(), await novo_usuario()
    agora = datetime.now(UTC)
    antiga = await _criar_revisao(
        engine_app, como, dono, motivo="odd", criado_em=agora - timedelta(days=3)
    )
    meio = await _criar_revisao(
        engine_app, como, dono, motivo="stake", criado_em=agora - timedelta(days=2)
    )
    limite = await _criar_revisao(
        engine_app, como, dono, motivo="odd", criado_em=agora - timedelta(days=1)
    )
    resolvida = await _criar_revisao(
        engine_app, como, dono, motivo="odd", criado_em=agora - timedelta(hours=12)
    )
    alheia = await _criar_revisao(
        engine_app, como, outro, motivo="odd", criado_em=agora - timedelta(days=4)
    )
    async with como(engine_app, dono) as session:
        marcada = await RevisaoPendenteRepo().resolve(session, dono, resolvida.revisao_id)
        assert marcada is not None
        await session.commit()

    async with como(engine_app, dono) as session:
        repo = RevisaoPendenteRepo()
        primeira, total = await repo.list_page(session, dono, {}, 1, 2)
        pagina_dois, total_segunda = await repo.list_page(session, dono, {}, 2, 2)
        alem, total_alem = await repo.list_page(session, dono, {}, 9, 2)
        intervalo, total_intervalo = await repo.list_page(
            session,
            dono,
            {
                "motivo": "odd",
                "desde": agora - timedelta(days=3),
                "ate": agora - timedelta(days=1),
            },
            1,
            50,
        )
        stats = await repo.stats(session, dono)

    assert [item.id for item in primeira] == [antiga.revisao_id, meio.revisao_id]
    assert [item.id for item in pagina_dois] == [limite.revisao_id]
    assert (total, total_segunda, total_alem) == (3, 3, 3)
    assert alem == []
    assert [item.id for item in intervalo] == [antiga.revisao_id]
    assert total_intervalo == 1
    assert stats.total == 3
    assert stats.por_motivo == {"odd": 2, "stake": 1}
    assert stats.mais_antiga_em == agora - timedelta(days=3)
    assert 3 * 86_400 <= stats.idade_maxima_segundos < 3 * 86_400 + 30

    async with como(engine_app, outro) as session:
        repo = RevisaoPendenteRepo()
        do_dono, total_do_dono = await repo.list_page(session, dono, {}, 1, 50)
        minhas, total_minhas = await repo.list_page(session, outro, {}, 1, 50)
        detalhe_do_dono = await repo.get_by_id(session, dono, antiga.revisao_id)
    assert (do_dono, total_do_dono, detalhe_do_dono) == ([], 0, None)
    assert ([item.id for item in minhas], total_minhas) == ([alheia.revisao_id], 1)


async def test_corrigir_atualiza_confirma_a_aposta_e_um_segundo_clique_nao_duplica(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    cenario = await _criar_revisao(engine_app, como, usuario_id, motivo="odd duvidosa")

    resposta = await _resolver(
        engine_app,
        como,
        usuario_id,
        cenario.revisao_id,
        "CORRIGIR",
        {"odd": 2.25},
    )
    repetida = await _resolver(
        engine_app,
        como,
        usuario_id,
        cenario.revisao_id,
        "CORRIGIR",
        {"odd": 3.0},
    )

    assert resposta.status_code == 200
    assert _json(resposta)["acao"] == "CORRIGIR"
    assert repetida.status_code == 409
    async with como(engine_app, usuario_id) as session:
        aposta = await ApostaRepo().get_by_chave(session, usuario_id, cenario.chave)
        revisao = await RevisaoPendenteRepo().get_by_id(session, usuario_id, cenario.revisao_id)
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario_id, cenario.chave)

    assert aposta is not None
    assert aposta.odd == pytest.approx(2.25)
    assert aposta.revisao_grave is False
    assert aposta.selecionada is True
    assert revisao is not None and revisao.resolvido_em is not None
    assert [evento.tipo for evento in eventos].count("REVISAO_RESOLVIDA") == 1
    resolucao = next(evento for evento in eventos if evento.tipo == "REVISAO_RESOLVIDA")
    assert resolucao.payload_json == {
        "revisao_id": cenario.revisao_id,
        "acao": "CORRIGIR",
        "motivo": "odd duvidosa",
        "campos_corrigidos": ["odd"],
    }


async def test_descartar_limpa_a_revisao_e_desmarca_sem_apagar_o_historico(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    cenario = await _criar_revisao(engine_app, como, usuario_id, motivo="não era uma aposta")

    resposta = await _resolver(engine_app, como, usuario_id, cenario.revisao_id, "DESCARTAR")

    assert resposta.status_code == 200
    async with como(engine_app, usuario_id) as session:
        aposta = await ApostaRepo().get_by_chave(session, usuario_id, cenario.chave)
        revisao = await RevisaoPendenteRepo().get_by_id(session, usuario_id, cenario.revisao_id)
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario_id, cenario.chave)
    assert aposta is not None
    assert (aposta.revisao_grave, aposta.selecionada) == (False, False)
    assert revisao is not None and revisao.resolvido_em is not None
    assert eventos[0].tipo == "APOSTA_CRIADA"
    assert [evento.tipo for evento in eventos].count("REVISAO_RESOLVIDA") == 1
    assert any(evento.tipo == "APOSTA_CANCELADA" for evento in eventos)


async def test_revisao_orfa_e_revisao_alheia_nao_mutam_nada(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    dono, outro = await novo_usuario(), await novo_usuario()
    orfa = await _criar_revisao(engine_app, como, dono, motivo="legado", com_aposta=False)

    alheia = await _resolver(engine_app, como, outro, orfa.revisao_id, "DESCARTAR")
    orfa_resposta = await _resolver(engine_app, como, dono, orfa.revisao_id, "DESCARTAR")

    assert alheia.status_code == 404
    assert orfa_resposta.status_code == 409
    async with como(engine_app, dono) as session:
        revisao = await RevisaoPendenteRepo().get_by_id(session, dono, orfa.revisao_id)
        eventos = await EventoRepo().list_by_usuario(session, dono)
    assert revisao is not None and revisao.resolvido_em is None
    assert eventos == []


async def test_aposta_sem_evento_de_criacao_fica_intacta_e_a_revisao_aberta(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    cenario = await _criar_revisao(
        engine_app,
        como,
        usuario_id,
        motivo="histórico legado incompleto",
        com_historico=False,
    )

    resposta = await _resolver(
        engine_app,
        como,
        usuario_id,
        cenario.revisao_id,
        "CORRIGIR",
        {"odd": 2.25},
    )

    assert resposta.status_code == 409
    async with como(engine_app, usuario_id) as session:
        aposta = await ApostaRepo().get_by_chave(session, usuario_id, cenario.chave)
        revisao = await RevisaoPendenteRepo().get_by_id(session, usuario_id, cenario.revisao_id)
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario_id, cenario.chave)
    assert aposta is not None and aposta.odd == pytest.approx(1.8)
    assert revisao is not None and revisao.resolvido_em is None
    assert eventos == []


async def test_stats_never_report_negative_aging(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    await _criar_revisao(
        engine_app,
        como,
        usuario_id,
        motivo="relógio adiantado",
        criado_em=datetime.now(UTC) + timedelta(minutes=5),
    )

    async with como(engine_app, usuario_id) as session:
        stats = await RevisaoPendenteRepo().stats(session, usuario_id)

    assert stats.total == 1
    assert stats.idade_maxima_segundos == 0


async def test_foto_exige_a_revisao_aberta_do_mesmo_usuario(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    dono, outro = await novo_usuario(), await novo_usuario()
    conteudo = b"\xff\xd8\xffimagem-da-aposta\xff\xd9"
    cenario = await _criar_revisao(engine_app, como, dono, motivo="conferir imagem", foto=conteudo)

    async with como(engine_app, dono) as session:
        usuario = await _usuario(session, dono)
        foto = await rota.ver_foto(cenario.revisao_id, usuario, session)
    async with como(engine_app, outro) as session:
        usuario = await _usuario(session, outro)
        alheia = await rota.ver_foto(cenario.revisao_id, usuario, session)

    assert foto.status_code == 200
    assert foto.body == conteudo
    assert foto.media_type == "image/jpeg"
    assert foto.headers["cache-control"] == "private, no-store"
    assert foto.headers["x-content-type-options"] == "nosniff"
    assert alheia.status_code == 404

    async with como(engine_app, dono) as session:
        resolvida = await RevisaoPendenteRepo().resolve(session, dono, cenario.revisao_id)
        assert resolvida is not None
        await session.commit()
    async with como(engine_app, dono) as session:
        usuario = await _usuario(session, dono)
        depois = await rota.ver_foto(cenario.revisao_id, usuario, session)
    assert depois.status_code == 404


async def test_falha_no_evento_de_auditoria_desfaz_correcao_e_resolucao(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usuario_id = await novo_usuario()
    cenario = await _criar_revisao(engine_app, como, usuario_id, motivo="odd duvidosa")
    append_real = EventoRepo.append

    async def falhar_no_evento(
        self: EventoRepo, session: AsyncSession, dados: dict[str, object]
    ) -> object:
        if dados["tipo"] == "REVISAO_RESOLVIDA":
            raise RuntimeError("evento de auditoria sabotado")
        return await append_real(self, session, dados)

    monkeypatch.setattr(EventoRepo, "append", falhar_no_evento)
    with pytest.raises(RuntimeError, match="sabotado"):
        await _resolver(
            engine_app,
            como,
            usuario_id,
            cenario.revisao_id,
            "CORRIGIR",
            {"odd": 2.5},
        )

    async with como(engine_app, usuario_id) as session:
        aposta = await ApostaRepo().get_by_chave(session, usuario_id, cenario.chave)
        revisao = await RevisaoPendenteRepo().get_by_id(session, usuario_id, cenario.revisao_id)
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario_id, cenario.chave)
    assert aposta is not None and aposta.odd == pytest.approx(1.8)
    assert aposta.revisao_grave is True
    assert revisao is not None and revisao.resolvido_em is None
    assert [evento.tipo for evento in eventos] == ["APOSTA_CRIADA"]


async def test_trava_do_materializador_recusa_a_resolucao_sem_escrever(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: NovoUsuario,
) -> None:
    usuario_id = await novo_usuario()
    cenario = await _criar_revisao(engine_app, como, usuario_id, motivo="odd duvidosa")

    async with como(engine_app, usuario_id) as segurando:
        await segurando.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
            {"chave": f"{usuario_id}:{cenario.chave}"},
        )
        ocupada = await _resolver(
            engine_app,
            como,
            usuario_id,
            cenario.revisao_id,
            "CORRIGIR",
            {"odd": 2.0},
        )
        await segurando.rollback()

    assert ocupada.status_code == 409
    async with como(engine_app, usuario_id) as session:
        aposta = await ApostaRepo().get_by_chave(session, usuario_id, cenario.chave)
        revisao = await RevisaoPendenteRepo().get_by_id(session, usuario_id, cenario.revisao_id)
        eventos = await EventoRepo().list_by_aposta_chave(session, usuario_id, cenario.chave)
    assert aposta is not None and aposta.odd == pytest.approx(1.8)
    assert revisao is not None and revisao.resolvido_em is None
    assert [evento.tipo for evento in eventos] == ["APOSTA_CRIADA"]
