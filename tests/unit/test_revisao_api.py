from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bancaemdia.api.deps import get_current_user, get_current_user_snapshot
from bancaemdia.api.v1 import apostas as apostas_api
from bancaemdia.api.v1 import revisao as rota
from bancaemdia.db.session import get_db, get_db_snapshot
from bancaemdia.domain.registros import (
    Aposta,
    EstatisticasRevisao,
    Evento,
    FotoRevisao,
    RevisaoPendente,
    Usuario,
)

USUARIO = 7
CHAVE = "t:100:200:0"
AGORA = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _revisao(**extra: Any) -> RevisaoPendente:
    campos = {
        "id": 8,
        "usuario_id": USUARIO,
        "midia_hash": "hash-da-foto",
        "motivo": "odd duvidosa",
        "extracao_bruta": {"aposta_chave": CHAVE, "extracao": {"odd": 1.8}},
        "criado_em": AGORA,
        "resolvido_em": None,
    }
    return RevisaoPendente(**{**campos, **extra})


def _aposta(**extra: Any) -> Aposta:
    campos = {
        "id": 3,
        "usuario_id": USUARIO,
        "chave": CHAVE,
        "chat_id": 100,
        "message_id": 200,
        "ordem_na_mensagem": 0,
        "midia_hash": "hash-da-foto",
        "banca_id": None,
        "conta_casa_id": None,
        "tipster_id": None,
        "time_casa_id": None,
        "time_fora_id": None,
        "mercado_id": None,
        "competicao_id": None,
        "data_aposta": AGORA,
        "data_jogo": None,
        "stake_unidades": 1.0,
        "stake_centavos": 5_000,
        "valor_aposta_centavos": 5_000,
        "odd": 1.8,
        "retorno_centavos": None,
        "estado": "PENDENTE",
        "origem": "telegram",
        "freebet": False,
        "duvida_de_par": False,
        "parceira_chave": None,
        "duplicada_de": None,
        "revisao_grave": True,
        "selecionada": True,
        "criada_em": AGORA,
        "atualizada_em": AGORA,
    }
    return Aposta(**{**campos, **extra})


def _banco(
    *,
    revisao: RevisaoPendente | None = None,
    aposta: Aposta | None = None,
    trava_livre: bool = True,
    foto: FotoRevisao | None = None,
):
    class Banco:
        def __init__(self) -> None:
            self.revisao = _revisao() if revisao is None else revisao
            self.aposta = _aposta() if aposta is None else aposta
            self.tem_aposta = True
            self.tem_historico = True
            self.trava_livre = trava_livre
            self.foto = foto or FotoRevisao(b"JPEG", "image/jpeg")
            self.novos_eventos: list[tuple[str, str, dict[str, Any]]] = []
            self.sql: list[tuple[str, dict[str, object] | None]] = []
            self.filtros: dict[str, object] = {}
            self.paginacao = (0, 0)
            self.commits = 0
            self.rollbacks = 0

    banco = Banco()
    criado = {
        "origem": "telegram",
        "odd": 1.8,
        "stake_unidades": 1.0,
        "valor_unidade_centavos": 5_000,
        "selecionada": True,
        "revisao_motivo": "odd duvidosa",
        "revisao_grave": True,
        "data_aposta": AGORA.isoformat(),
    }

    class RevisaoRepo:
        async def get_by_id(self, session, usuario_id, id_):
            return banco.revisao if banco.revisao is not None and id_ == banco.revisao.id else None

        async def get_by_id_for_update(self, session, usuario_id, id_):
            return await self.get_by_id(session, usuario_id, id_)

        async def list_page(self, session, usuario_id, filtros, pagina, tamanho):
            banco.filtros = filtros
            banco.paginacao = (pagina, tamanho)
            revisoes = [] if banco.revisao is None else [banco.revisao]
            return revisoes, len(revisoes)

        async def stats(self, session, usuario_id):
            return EstatisticasRevisao(1, {"odd duvidosa": 1}, AGORA, 90)

        async def get_foto_by_id(self, session, usuario_id, id_):
            if banco.revisao is None or id_ != banco.revisao.id:
                return None
            return banco.foto

        async def resolve(self, session, usuario_id, id_):
            if banco.revisao is None or banco.revisao.resolvido_em is not None:
                return None
            banco.revisao = replace(banco.revisao, resolvido_em=AGORA)
            return banco.revisao

    class ApostaRepo:
        async def get_by_chave_for_update(self, session, usuario_id, chave):
            return banco.aposta if banco.tem_aposta and chave == CHAVE else None

        async def upsert_materializada(self, session, dados):
            if not banco.tem_aposta:
                return None
            banco.aposta = replace(
                banco.aposta,
                odd=dados.get("odd", banco.aposta.odd),
                stake_unidades=float(dados.get("stake_unidades", banco.aposta.stake_unidades)),
                stake_centavos=int(dados.get("stake_centavos", banco.aposta.stake_centavos)),
                valor_aposta_centavos=int(
                    dados.get("valor_aposta_centavos", banco.aposta.valor_aposta_centavos)
                ),
                revisao_grave=bool(dados.get("revisao_grave", banco.aposta.revisao_grave)),
                selecionada=bool(dados.get("selecionada", banco.aposta.selecionada)),
            )
            return banco.aposta

    class EventoRepo:
        async def list_by_aposta_chave(self, session, usuario_id, chave):
            criacao = [("APOSTA_CRIADA", "ia", criado)] if banco.tem_historico else []
            todos = [*criacao, *banco.novos_eventos]
            return [
                Evento(i, USUARIO, tipo, payload, fonte, None, None, None, AGORA, CHAVE)
                for i, (tipo, fonte, payload) in enumerate(todos, start=1)
            ]

        async def append(self, session, dados):
            banco.novos_eventos.append((
                str(dados["tipo"]),
                str(dados["fonte"]),
                dict(dados["payload_json"]),
            ))
            return Evento(
                len(banco.novos_eventos) + 1,
                USUARIO,
                str(dados["tipo"]),
                dict(dados["payload_json"]),
                str(dados["fonte"]),
                None,
                None,
                None,
                AGORA,
                CHAVE,
            )

    class ContaCasaRepo:
        async def get_by_id(self, session, usuario_id, id_):
            return None

    class Session:
        async def scalar(self, statement, params=None):
            banco.sql.append((str(statement), params))
            return banco.trava_livre

        async def commit(self):
            banco.commits += 1

        async def rollback(self):
            banco.rollbacks += 1

    class Sessoes:
        async def abrir(self):
            yield Session()

    banco.repos = {
        "RevisaoPendenteRepo": RevisaoRepo,
        "ApostaRepo": ApostaRepo,
        "EventoRepo": EventoRepo,
        "ContaCasaRepo": ContaCasaRepo,
    }
    banco.sessao = Sessoes().abrir
    return banco


def _cliente(monkeypatch, banco) -> TestClient:
    def usuario() -> Usuario:
        return Usuario(USUARIO, "p@teste.local", "P", AGORA, True)

    monkeypatch.setattr(rota, "RevisaoPendenteRepo", banco.repos["RevisaoPendenteRepo"])
    monkeypatch.setattr(rota, "ApostaRepo", banco.repos["ApostaRepo"])
    monkeypatch.setattr(apostas_api, "ApostaRepo", banco.repos["ApostaRepo"])
    monkeypatch.setattr(apostas_api, "EventoRepo", banco.repos["EventoRepo"])
    monkeypatch.setattr(apostas_api, "ContaCasaRepo", banco.repos["ContaCasaRepo"])
    app = FastAPI()
    app.include_router(rota.router)
    app.dependency_overrides[get_current_user] = usuario
    app.dependency_overrides[get_current_user_snapshot] = usuario
    app.dependency_overrides[get_db] = banco.sessao
    app.dependency_overrides[get_db_snapshot] = banco.sessao
    return TestClient(app)


def test_list_detail_and_stats_publish_the_review_contract(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)

    lista = cliente.get(
        "/api/v1/revisao",
        params={"motivo": " odd duvidosa ", "page": 2, "page_size": 10},
    )
    detalhe = cliente.get("/api/v1/revisao/8")
    stats = cliente.get("/api/v1/revisao/stats")

    assert lista.status_code == detalhe.status_code == stats.status_code == 200
    assert lista.json()["data"][0]["foto_url"] == "/api/v1/revisao/8/foto"
    assert lista.json()["pagination"] == {"page": 2, "page_size": 10, "total": 1}
    assert banco.filtros["motivo"] == "odd duvidosa"
    assert detalhe.json()["extracao_bruta"]["aposta_chave"] == CHAVE
    assert stats.json() == {
        "total": 1,
        "por_motivo": {"odd duvidosa": 1},
        "mais_antiga_em": AGORA.isoformat(),
        "idade_maxima_segundos": 90,
    }


def test_review_photo_uses_short_lived_s3_url_when_object_is_uploaded(monkeypatch) -> None:
    class Settings:
        S3_UPLOAD_BUCKET = "private-uploads"

    chamadas = []
    monkeypatch.setattr(rota, "get_settings", lambda: Settings())
    monkeypatch.setattr(
        rota,
        "signed_url",
        lambda bucket, key, tipo: chamadas.append((bucket, key, tipo)) or "https://signed.test",
    )
    assert rota.linha_da_revisao(_revisao(), ("media/abc", "image/png"))["foto_url"] == (
        "https://signed.test"
    )
    assert chamadas == [("private-uploads", "media/abc", "image/png")]
    assert rota.linha_da_revisao(_revisao())["foto_url"] == "/api/v1/revisao/8/foto"


def test_photo_is_private_not_sniffed_and_keeps_its_image_type(monkeypatch) -> None:
    resposta = _cliente(monkeypatch, _banco()).get("/api/v1/revisao/8/foto")

    assert resposta.status_code == 200
    assert resposta.content == b"JPEG"
    assert resposta.headers["content-type"] == "image/jpeg"
    assert resposta.headers["cache-control"] == "private, no-store"
    assert resposta.headers["x-content-type-options"] == "nosniff"


def test_correction_locks_updates_audits_resolves_and_commits_once(monkeypatch) -> None:
    banco = _banco()
    resposta = _cliente(monkeypatch, banco).post(
        "/api/v1/revisao/8/resolver",
        json={"acao": "corrigir", "aposta_corrigida": {"odd": 2.25}},
    )

    assert resposta.status_code == 200
    assert resposta.json()["acao"] == "CORRIGIR"
    assert resposta.json()["aposta"]["odd"] == pytest.approx(2.25)
    assert resposta.json()["eventos_gravados"] == 2
    assert [tipo for tipo, _, _ in banco.novos_eventos] == [
        "CORRECAO_MANUAL",
        "REVISAO_RESOLVIDA",
    ]
    assert banco.novos_eventos[-1][2] == {
        "revisao_id": 8,
        "acao": "CORRIGIR",
        "motivo": "odd duvidosa",
        "campos_corrigidos": ["odd"],
    }
    assert banco.commits == 1
    assert banco.rollbacks == 0
    assert banco.revisao is not None and banco.revisao.resolvido_em == AGORA
    travas = [params for sql, params in banco.sql if "pg_try_advisory_xact_lock" in sql]
    assert travas == [{"chave": f"{USUARIO}:{CHAVE}"}]


def test_discard_uses_safe_delete_before_audit(monkeypatch) -> None:
    banco = _banco()
    resposta = _cliente(monkeypatch, banco).post(
        "/api/v1/revisao/8/resolver",
        json={"acao": "DESCARTAR"},
    )

    assert resposta.status_code == 200
    assert [tipo for tipo, _, _ in banco.novos_eventos] == [
        "CORRECAO_MANUAL",
        "APOSTA_CANCELADA",
        "REVISAO_RESOLVIDA",
    ]
    assert resposta.json()["aposta"]["apagada"] is True
    assert banco.aposta.selecionada is False


def test_same_pair_requires_a_pending_pair_review_with_a_candidate(monkeypatch) -> None:
    banco = _banco()
    resposta = _cliente(monkeypatch, banco).post(
        "/api/v1/revisao/8/resolver", json={"acao": "MESMA"}
    )

    assert resposta.status_code == 422
    assert banco.novos_eventos == []


def test_busy_already_resolved_and_orphan_reviews_do_not_write(monkeypatch) -> None:
    ocupada = _banco(trava_livre=False)
    resposta_ocupada = _cliente(monkeypatch, ocupada).post(
        "/api/v1/revisao/8/resolver", json={"acao": "CORRIGIR"}
    )
    assert resposta_ocupada.status_code == 409
    assert ocupada.novos_eventos == []
    assert ocupada.commits == 0 and ocupada.rollbacks == 1

    resolvida = _banco(revisao=_revisao(resolvido_em=AGORA))
    resposta_resolvida = _cliente(monkeypatch, resolvida).post(
        "/api/v1/revisao/8/resolver", json={"acao": "CORRIGIR"}
    )
    assert resposta_resolvida.status_code == 409
    assert resposta_resolvida.json()["detail"] == rota.JA_RESOLVIDA
    assert resolvida.novos_eventos == []

    orfa = _banco()
    orfa.tem_aposta = False
    resposta_orfa = _cliente(monkeypatch, orfa).post(
        "/api/v1/revisao/8/resolver", json={"acao": "CORRIGIR"}
    )
    assert resposta_orfa.status_code == 409
    assert resposta_orfa.json()["detail"] == rota.ORFA
    assert orfa.novos_eventos == []


def test_a_materialized_bet_without_its_creation_history_is_left_untouched(monkeypatch) -> None:
    banco = _banco()
    banco.tem_historico = False

    resposta = _cliente(monkeypatch, banco).post(
        "/api/v1/revisao/8/resolver",
        json={"acao": "CORRIGIR", "aposta_corrigida": {"odd": 2.25}},
    )

    assert resposta.status_code == 409
    assert resposta.json()["detail"] == rota.HISTORICO_INCOMPLETO
    assert banco.aposta.odd == pytest.approx(1.8)
    assert banco.novos_eventos == []
    assert banco.commits == 0 and banco.rollbacks == 1


def test_invalid_resolution_is_rejected_before_the_append_only_log(monkeypatch) -> None:
    banco = _banco()
    resposta = _cliente(monkeypatch, banco).post(
        "/api/v1/revisao/8/resolver",
        json={"acao": "CORRIGIR", "aposta_corrigida": {"revisao_grave": False}},
    )

    assert resposta.status_code == 422
    assert "controlado pela fila" in resposta.json()["detail"]
    assert banco.novos_eventos == []
    assert banco.commits == 0 and banco.rollbacks == 1
