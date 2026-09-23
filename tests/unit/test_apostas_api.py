from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi.testclient import TestClient

from bancaemdia import main
from bancaemdia.api import deps
from bancaemdia.api.v1 import apostas as rota
from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db
from bancaemdia.domain.account_attribution import AccountResolution, ResolutionStatus
from bancaemdia.domain.materializar import MOTIVO_APAGADA, projetar
from bancaemdia.domain.registros import Aposta, ContaCasa, Evento, RevisaoPendente, Usuario

USUARIO = 7
CHAVE = "t:100:5:0"
AGORA = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
CRIACAO = {
    "origem": "telegram",
    "casa": "Betano",
    "evento": "Internacional x Corinthians",
    "descricao": "Mais de 2.5",
    "mercado_bruto": "Total de gols",
    "odd": 1.82,
    "stake_unidades": 1.0,
    "valor_unidade_centavos": 10_000,
    "freebet": False,
    "selecionada": True,
    "chat_id": 100,
    "message_id": 5,
    "ordem_na_mensagem": 0,
    "data_aposta": "2026-09-10T21:00:00",
}


@pytest.fixture(scope="module")
def chave_rsa():
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = par.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    return privada, {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(par.public_key())),
        "kid": "k1",
    }


def _token(privada, sub=str(USUARIO)):
    settings = get_settings()
    corpo = {
        "sub": sub,
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "exp": int(time.time()) + 60,
    }
    return jwt.encode(corpo, privada, algorithm="RS256", headers={"kid": "k1"})


def _chaves(jwk_publica):
    def responder(request):
        return httpx.Response(200, json={"keys": [jwk_publica]})

    return auth_jwt.JWKSCache("https://issuer.test/jwks", "RS256", httpx.MockTransport(responder))


def _aposta(**extra) -> Aposta:
    campos = {
        "id": 1,
        "usuario_id": USUARIO,
        "chave": CHAVE,
        "chat_id": 100,
        "message_id": 5,
        "ordem_na_mensagem": 0,
        "midia_hash": "h1",
        "banca_id": None,
        "conta_casa_id": None,
        "tipster_id": None,
        "time_casa_id": None,
        "time_fora_id": None,
        "mercado_id": None,
        "competicao_id": None,
        "data_aposta": datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
        "data_jogo": None,
        "stake_unidades": 1.0,
        "stake_centavos": 10_000,
        "valor_aposta_centavos": 10_000,
        "odd": 1.82,
        "retorno_centavos": None,
        "estado": "PENDENTE",
        "origem": "telegram",
        "freebet": False,
        "duvida_de_par": False,
        "parceira_chave": None,
        "duplicada_de": None,
        "revisao_grave": False,
        "selecionada": True,
        "criada_em": AGORA,
        "atualizada_em": AGORA,
    }
    return Aposta(**{**campos, **extra})


def _banco(
    historico=None,
    aposta=None,
    trava_livre=True,
    upsert_vence=True,
    revisoes=(),
    total=1,
    referencias_canonicas_ausentes=(),
):
    class Banco:
        def __init__(self):
            self.eventos = list(historico or [("APOSTA_CRIADA", "ia", dict(CRIACAO))])
            self.gravados = []
            self.commits = 0
            self.rollbacks = 0
            self.sql = []
            self.consultas_de_conta = []
            self.resolvidas = []

    banco = Banco()
    linha = _aposta() if aposta is None else aposta

    class ApostaRepo:
        async def get_by_chave(self, session, usuario_id, chave):
            return linha if linha is not None and chave == linha.chave else None

        async def get_by_chave_for_update(self, session, usuario_id, chave):
            return await self.get_by_chave(session, usuario_id, chave)

        async def list_page(self, session, usuario_id, filtros, pagina, tamanho):
            banco.filtros = filtros
            banco.paginacao = (pagina, tamanho)
            return ([linha] if linha is not None else []), total

        async def upsert_materializada(self, session, dados):
            banco.gravados.append(dados)
            if not upsert_vence:
                return None
            campos = {c: v for c, v in dados.items() if c in Aposta.__dataclass_fields__}
            return _aposta(**campos)

    class EventoRepo:
        async def append(self, session, dados):
            banco.eventos.append((dados["tipo"], dados["fonte"], dados["payload_json"]))
            return Evento(
                id=len(banco.eventos),
                usuario_id=USUARIO,
                tipo=dados["tipo"],
                fonte=dados["fonte"],
                payload_json=dados["payload_json"],
                confianca=dados.get("confianca"),
                criado_em=AGORA,
                chat_id=dados.get("chat_id"),
                message_id=dados.get("message_id"),
                aposta_chave=dados.get("aposta_chave"),
            )

        async def list_by_aposta_chave(self, session, usuario_id, chave):
            return [
                Evento(
                    id=i,
                    usuario_id=USUARIO,
                    tipo=tipo,
                    fonte=fonte,
                    payload_json=payload,
                    confianca=None,
                    criado_em=AGORA,
                    chat_id=None,
                    message_id=None,
                    aposta_chave=chave,
                )
                for i, (tipo, fonte, payload) in enumerate(banco.eventos, start=1)
            ]

    class RevisaoPendenteRepo:
        async def list_abertas_by_aposta_chave(self, session, usuario_id, chave):
            return list(revisoes)

        async def resolve_superseded(self, session, usuario_id, chave, motivo):
            banco.resolvidas.append((chave, motivo))

    class ContaCasaRepo:
        async def get_by_id(self, session, usuario_id, id_):
            banco.consultas_de_conta.append((usuario_id, id_))
            if id_ != 42:
                return None
            return ContaCasa(
                id=42,
                usuario_id=USUARIO,
                casa_id=1,
                apelido="principal",
                desde=None,
                ate=None,
                ativa=True,
            )

        async def get_vigente_by_nome_da_casa(self, session, usuario_id, nome, data):
            return await self.get_by_id(session, usuario_id, 42)

    class CasaRepo:
        async def get_id_by_nome(self, session, nome):
            return 1

    class UnidadeRepo:
        async def get_vigente(self, session, usuario_id, quando):
            return None

    class Session:
        async def execute(self, statement, params=None):
            banco.sql.append((str(statement), params))

        async def scalar(self, statement, params=None):
            sql = str(statement)
            banco.sql.append((sql, params))
            for tabela in ("tipsters", "times", "mercados", "competicoes"):
                if f"FROM {tabela}" in sql:
                    return None if tabela in referencias_canonicas_ausentes else 1
            return trava_livre

        async def commit(self):
            banco.commits += 1

        async def rollback(self):
            banco.rollbacks += 1

    class Sessoes:
        async def abrir(self):
            yield Session()

    banco.repos = {
        "ApostaRepo": ApostaRepo,
        "EventoRepo": EventoRepo,
        "RevisaoPendenteRepo": RevisaoPendenteRepo,
        "ContaCasaRepo": ContaCasaRepo,
        "CasaRepo": CasaRepo,
        "UnidadeRepo": UnidadeRepo,
    }
    banco.sessao = Sessoes().abrir
    return banco


def _cliente(monkeypatch, chave_rsa, banco, usuario_id=USUARIO):
    _, publica = chave_rsa

    class UsuarioRepo:
        async def get_by_id(self, session, id_):
            if id_ != usuario_id:
                return None
            return Usuario(id=id_, email="p@teste.local", nome="P", criado_em=AGORA, ativo=True)

    for nome, classe in banco.repos.items():
        monkeypatch.setattr(rota, nome, classe)

    async def attribute_account(session, usuario_id, casa, instant, explicit_id=None):
        conta = await rota.ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, usuario_id, casa, instant
        )
        return AccountResolution(
            ResolutionStatus.NONE if conta is None else ResolutionStatus.UNIQUE,
            None if conta is None else conta.id,
        )

    async def sync_account_review(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(rota, "attribute_account", attribute_account)
    monkeypatch.setattr(rota, "sync_account_review", sync_account_review)
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: _chaves(publica))
    monkeypatch.setattr(deps, "UsuarioRepo", UsuarioRepo)
    monkeypatch.setitem(main.app.dependency_overrides, get_db, banco.sessao)
    return TestClient(main.app)


def _cabecalho(privada):
    return {"Authorization": f"Bearer {_token(privada)}"}


def test_the_list_answers_with_the_bets_and_the_page(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(total=137)
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.get("/api/v1/apostas?page=2&page_size=25", headers=_cabecalho(privada))

    corpo = resposta.json()
    assert resposta.status_code == 200
    assert corpo["pagination"] == {"page": 2, "page_size": 25, "total": 137}
    assert corpo["data"][0]["chave"] == CHAVE
    assert banco.paginacao == (2, 25)


def test_the_list_passes_every_filter_the_person_asked_for(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.get(
        "/api/v1/apostas?estado=GREEN&casa_id=3&tipster_id=4&mercado_id=5&competicao_id=6"
        "&origem=telegram&revisao_grave=true&desde=2026-09-01T00:00:00Z",
        headers=_cabecalho(privada),
    )

    assert banco.filtros["estado"] == "GREEN"
    assert (banco.filtros["casa_id"], banco.filtros["tipster_id"]) == (3, 4)
    assert (banco.filtros["mercado_id"], banco.filtros["competicao_id"]) == (5, 6)
    assert banco.filtros["origem"] == "telegram"
    assert banco.filtros["revisao_grave"] is True
    assert banco.filtros["desde"] == datetime(2026, 9, 1, tzinfo=UTC)
    # A apagada só aparece quando a pessoa pede.
    assert banco.filtros["incluir_apagadas"] is False


def test_a_page_bigger_than_the_cap_is_refused(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    cliente = _cliente(monkeypatch, chave_rsa, _banco())

    assert cliente.get(
        "/api/v1/apostas?page_size=101", headers=_cabecalho(privada)
    ).status_code == (422)
    assert cliente.get("/api/v1/apostas?page=0", headers=_cabecalho(privada)).status_code == 422


def test_the_list_needs_a_token(monkeypatch, chave_rsa) -> None:
    cliente = _cliente(monkeypatch, chave_rsa, _banco())

    assert cliente.get("/api/v1/apostas").status_code == 401


def test_the_detail_brings_the_bet_its_picks_its_history_and_its_review(
    monkeypatch, chave_rsa
) -> None:
    privada, _ = chave_rsa
    revisao = RevisaoPendente(
        id=9,
        usuario_id=USUARIO,
        midia_hash="h1",
        motivo="coerência das odds",
        extracao_bruta={"aposta_chave": CHAVE},
        criado_em=AGORA,
        resolvido_em=None,
    )
    banco = _banco(revisoes=[revisao])
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.get(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada)).json()

    assert corpo["aposta"]["chave"] == CHAVE
    assert corpo["selecoes"] == {
        "descricao": "Mais de 2.5",
        "mercado": "Total de gols",
        "evento": "Internacional x Corinthians",
        "casa": "Betano",
    }
    assert [e["tipo"] for e in corpo["eventos"]] == ["APOSTA_CRIADA"]
    assert corpo["revisao_pendente"]["motivo"] == "coerência das odds"


def test_the_bet_of_another_person_is_not_found(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    # Sob a RLS a aposta de outra pessoa é invisível, então ela não existe para quem pergunta.
    banco = _banco(aposta=None)
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.get("/api/v1/apostas/t:1:1:0", headers=_cabecalho(privada))

    assert resposta.status_code == 404
    assert resposta.json() == {"detail": "não achei esta aposta"}


def test_a_correction_becomes_one_event_with_only_what_changed(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        f"/api/v1/apostas/{CHAVE}",
        json={"odd": 1.95, "stake_unidades": 1.0, "tipster_id": 8},
        headers=_cabecalho(privada),
    )

    corpo = resposta.json()
    assert resposta.status_code == 200
    assert corpo["eventos_gravados"] == 1
    tipo, fonte, payload = banco.eventos[-1]
    assert (tipo, fonte) == ("CORRECAO_MANUAL", "manual")
    # A stake não mudou: só entra no histórico o que mudou.
    assert payload == {"odd": 1.95, "tipster_id": 8}
    assert corpo["aposta"]["odd"] == pytest.approx(1.95)


def test_a_correction_that_changes_nothing_writes_nothing(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={"odd": 1.82}, headers=_cabecalho(privada)
    ).json()

    assert corpo["eventos_gravados"] == 0
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]


def test_a_key_the_person_did_not_send_is_not_a_null(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(historico=[("APOSTA_CRIADA", "ia", dict(CRIACAO, tipster_id=3))])
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.patch(f"/api/v1/apostas/{CHAVE}", json={"odd": 1.95}, headers=_cabecalho(privada))
    sem_tipster = banco.eventos[-1][2]
    cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={"tipster_id": None}, headers=_cabecalho(privada)
    )

    # Campo ausente é "não falei disso"; `null` de propósito é "apague este".
    assert "tipster_id" not in sem_tipster
    assert banco.eventos[-1][2] == {"tipster_id": None}


def test_a_field_that_is_not_the_persons_to_change_is_refused(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={"retorno_centavos": 999}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 422
    assert "campo que não pode ser corrigido" in resposta.json()["detail"]
    # A lista é conferida ANTES de qualquer escrita: `eventos` não se apaga.
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]


def test_an_account_that_is_not_yours_is_refused_before_the_write(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={"conta_casa_id": 99}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 422
    assert resposta.json()["detail"] == "conta_casa_id não existe nas suas contas"
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]


@pytest.mark.parametrize(
    ("campo", "tabela"),
    [
        ("tipster_id", "tipsters"),
        ("time_casa_id", "times"),
        ("time_fora_id", "times"),
        ("mercado_id", "mercados"),
        ("competicao_id", "competicoes"),
    ],
)
def test_an_unknown_shared_reference_is_refused_before_the_write(
    monkeypatch, chave_rsa, campo, tabela
) -> None:
    privada, _ = chave_rsa
    banco = _banco(referencias_canonicas_ausentes={tabela})
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={campo: 99}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 422
    assert resposta.json()["detail"] == f"{campo} não existe no catálogo compartilhado"
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]


@pytest.mark.parametrize(
    "campo",
    [
        "conta_casa_id",
        "tipster_id",
        "time_casa_id",
        "time_fora_id",
        "mercado_id",
        "competicao_id",
    ],
)
def test_a_boolean_is_never_accepted_as_a_reference_id(monkeypatch, chave_rsa, campo) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={campo: True}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 422
    assert resposta.json()["detail"] == f"{campo} tem de ser um número"
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]
    assert banco.consultas_de_conta == []
    assert banco.sql == []
    assert banco.gravados == []
    assert banco.commits == 0


@pytest.mark.parametrize("valor", [2**63, -(2**63) - 1])
@pytest.mark.parametrize(
    "campo",
    [
        "conta_casa_id",
        "tipster_id",
        "time_casa_id",
        "time_fora_id",
        "mercado_id",
        "competicao_id",
    ],
)
def test_an_id_outside_bigint_is_refused_before_any_database_access(
    monkeypatch, chave_rsa, campo, valor
) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)
    ids = {
        "conta_casa_id": 42,
        "tipster_id": 1,
        "time_casa_id": 1,
        "time_fora_id": 1,
        "mercado_id": 1,
        "competicao_id": 1,
    }
    ids[campo] = valor

    resposta = cliente.patch(f"/api/v1/apostas/{CHAVE}", json=ids, headers=_cabecalho(privada))

    assert resposta.status_code == 422
    assert resposta.json()["detail"] == f"{campo} tem de caber em um BIGINT"
    assert banco.consultas_de_conta == []
    assert banco.sql == []
    assert banco.gravados == []
    assert banco.commits == 0
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]


def test_the_state_is_only_corrected_on_a_bet_marked_for_review(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={"estado": "GREEN"}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 422
    assert "marcada para revisão" in resposta.json()["detail"]


def test_settling_a_bet_writes_the_result_and_the_money_it_implies(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "GREEN"}, headers=_cabecalho(privada)
    ).json()

    tipo, fonte, payload = banco.eventos[-1]
    assert (tipo, fonte, payload) == ("RESULTADO_REGISTRADO", "manual", {"estado": "GREEN"})
    assert corpo["aposta"]["estado"] == "GREEN"
    assert corpo["aposta"]["retorno_centavos"] == 18_200
    assert corpo["aposta"]["lucro_centavos"] == 8_200


def test_settling_a_bet_never_writes_a_cashbox_line(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "GREEN"}, headers=_cabecalho(privada)
    )

    # O saldo da casa já conta o retorno da aposta: lançar um movimento contaria o ganho duas vezes.
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA", "RESULTADO_REGISTRADO"]
    assert not any("movimento" in str(sql).lower() for sql, _ in banco.sql)


def test_a_cashout_carries_the_value_the_house_paid(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado",
        json={"estado": "CASHOUT", "cashout_valor_centavos": 12_345},
        headers=_cabecalho(privada),
    ).json()

    assert banco.eventos[-1][0] == "CASHOUT_REGISTRADO"
    assert corpo["aposta"]["retorno_centavos"] == 12_345


def test_a_cashout_without_its_value_is_refused(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado",
        json={"estado": "CASHOUT"},
        headers=_cabecalho(privada),
    )

    assert resposta.status_code == 422
    assert "cashout precisa do valor" in resposta.json()["detail"]


def test_deleting_takes_the_bet_out_and_the_second_click_adds_nothing(
    monkeypatch, chave_rsa
) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    primeiro = cliente.delete(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada)).json()
    segundo = cliente.delete(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada)).json()

    assert banco.eventos[1] == ("APOSTA_CANCELADA", "manual", {"motivo": MOTIVO_APAGADA})
    assert primeiro["eventos_gravados"] == 1
    assert primeiro["aposta"]["apagada"] is True
    assert segundo["eventos_gravados"] == 0


def test_a_deleted_bet_comes_back_when_the_person_undoes_it(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(
        historico=[
            ("APOSTA_CRIADA", "ia", dict(CRIACAO)),
            ("APOSTA_CANCELADA", "manual", {"motivo": MOTIVO_APAGADA}),
        ],
        aposta=_aposta(selecionada=False),
    )
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.post(f"/api/v1/apostas/{CHAVE}/restaurar", headers=_cabecalho(privada)).json()

    assert banco.eventos[-1] == ("SELECAO_ALTERADA", "manual", {"selecionada": True})
    assert corpo["aposta"]["apagada"] is False


def test_a_bet_someone_else_is_writing_answers_that_it_is_busy(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(trava_livre=False)
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "RED"}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 409
    assert "está sendo lida agora" in resposta.json()["detail"]
    assert [t for t, _, _ in banco.eventos] == ["APOSTA_CRIADA"]
    assert banco.commits == 0


def test_a_write_that_lost_the_race_is_not_reported_as_done(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(upsert_vence=False)
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "RED"}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 409
    assert (banco.commits, banco.rollbacks) == (0, 1)


def test_every_write_takes_the_same_key_the_worker_takes(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.delete(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada))

    travas = [params for sql, params in banco.sql if "pg_try_advisory_xact_lock" in sql]
    assert travas == [{"chave": f"{USUARIO}:{CHAVE}"}]


def test_a_correction_that_answers_the_review_takes_it_off_the_queue(
    monkeypatch, chave_rsa
) -> None:
    privada, _ = chave_rsa
    banco = _banco(
        historico=[
            ("APOSTA_CRIADA", "ia", dict(CRIACAO, revisao_motivo="a foto tem 2 cupons")),
        ]
    )
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.patch(
        f"/api/v1/apostas/{CHAVE}",
        json={"revisao_motivo": None},
        headers=_cabecalho(privada),
    )

    assert banco.resolvidas == [(CHAVE, None)]


def test_creating_a_bet_by_hand_needs_a_house_it_knows(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    cliente = _cliente(monkeypatch, chave_rsa, _banco())

    sem_casa = cliente.post(
        "/api/v1/apostas", json={"odd": 2.0, "stake_unidades": 1.0}, headers=_cabecalho(privada)
    )
    casa_estranha = cliente.post(
        "/api/v1/apostas",
        json={"casa": "Casa Inventada", "odd": 2.0, "stake_unidades": 1.0},
        headers=_cabecalho(privada),
    )

    assert sem_casa.status_code == 422 and sem_casa.json()["detail"] == "falta a casa de apostas"
    assert casa_estranha.status_code == 422
    assert "não conheço a casa" in casa_estranha.json()["detail"]


def test_a_bet_created_by_hand_is_born_from_its_own_creation_event(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.post(
        "/api/v1/apostas",
        json={
            "casa": "betano",
            "odd": 2.0,
            "stake_unidades": 1.5,
            "evento": "Flamengo x Vasco",
            "descricao": "Mais de 1.5",
        },
        headers=_cabecalho(privada),
    )

    corpo = resposta.json()["aposta"]
    tipo, fonte, payload = banco.eventos[-1]
    assert resposta.status_code == 201
    assert (tipo, fonte) == ("APOSTA_CRIADA", "manual")
    assert payload["casa"] == "Betano" and payload["origem"] == "manual"
    assert corpo["chave"].startswith("m:")
    assert corpo["stake_centavos"] == 15_000
    assert corpo["conta_casa_id"] == 42
    assert corpo["conta_atribuicao"] == "ASSIGNED"
    assert resposta.json()["casa_id"] == 1
    assert "aviso" not in resposta.json()


def test_manual_account_reference_is_saved_and_cross_house_is_rejected(
    monkeypatch, chave_rsa
) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)
    valid = cliente.post(
        "/api/v1/apostas",
        json={"casa": "betano", "odd": 2.0, "stake_unidades": 1.0, "conta_casa_id": 42},
        headers=_cabecalho(privada),
    )
    assert valid.status_code == 201
    assert banco.eventos[-1][2]["conta_referencia_explicita"] is True
    assert banco.eventos[-1][2]["conta_casa_id"] == 42
    events_before_rejection = len(banco.eventos)

    from bancaemdia.domain.account_attribution_service import InvalidAccountReferenceError

    async def invalid(*args, **kwargs):
        await asyncio.sleep(0)
        raise InvalidAccountReferenceError("conta_casa_id não pertence a você nesta casa")

    monkeypatch.setattr(rota, "attribute_account", invalid)
    rejected = cliente.post(
        "/api/v1/apostas",
        json={"casa": "betano", "odd": 2.0, "stake_unidades": 1.0, "conta_casa_id": 42},
        headers=_cabecalho(privada),
    )
    assert rejected.status_code == 422
    assert len(banco.eventos) == events_before_rejection


def test_a_bet_created_without_a_date_uses_brazil_time(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    instante = datetime(2026, 9, 22, 14, 30, tzinfo=rota.FUSO_DO_BRASIL)

    class Relogio(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is rota.FUSO_DO_BRASIL
            return instante

    monkeypatch.setattr(rota, "datetime", Relogio)
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.post(
        "/api/v1/apostas",
        json={"casa": "betano", "odd": 2.0, "stake_unidades": 1.0},
        headers=_cabecalho(privada),
    )

    assert resposta.status_code == 201
    assert banco.eventos[-1][2]["data_aposta"] == instante.isoformat()
    assert banco.gravados[-1]["data_aposta"] == instante


def test_the_bet_written_by_the_route_carries_what_the_history_says(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "GREEN"}, headers=_cabecalho(privada)
    )

    gravado = banco.gravados[-1]
    esperado, _ = projetar(banco.eventos)
    # A linha é o retrato do histórico: origem e mensagem vêm dele, não do pedido.
    assert gravado["origem"] == "telegram"
    assert (gravado["chat_id"], gravado["message_id"]) == (100, 5)
    assert gravado["estado"] == esperado["estado"]
    assert gravado["retorno_centavos"] == esperado["retorno_centavos"]
    assert gravado["selecionada"] is True


def test_the_routes_of_this_issue_are_registered() -> None:
    # O FastAPI aninha os roteadores incluídos; quem lista os caminhos de verdade é o esquema.
    caminhos = main.app.openapi()["paths"]

    assert set(caminhos["/api/v1/apostas"]) == {"get", "post"}
    assert set(caminhos["/api/v1/apostas/{chave}"]) == {"get", "patch", "delete"}
    assert "post" in caminhos["/api/v1/apostas/{chave}/resultado"]
    assert "post" in caminhos["/api/v1/apostas/{chave}/restaurar"]


def test_a_bet_that_is_not_there_is_not_written(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(aposta=None)
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    resposta = cliente.patch(
        "/api/v1/apostas/t:9:9:0", json={"odd": 2.0}, headers=_cabecalho(privada)
    )

    assert resposta.status_code == 404
    assert banco.gravados == []


def test_the_person_can_ask_for_the_deleted_ones(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.get("/api/v1/apostas?incluir_apagadas=true", headers=_cabecalho(privada))

    assert banco.filtros["incluir_apagadas"] is True


def test_an_inactive_account_cannot_write(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco, usuario_id=999)

    resposta = cliente.delete(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada))

    assert resposta.status_code == 401
    assert banco.gravados == []


def test_the_answer_of_a_bet_shows_the_money_in_cents_and_the_dates_as_text(
    monkeypatch, chave_rsa
) -> None:
    privada, _ = chave_rsa
    banco = _banco(aposta=_aposta(estado="GREEN", retorno_centavos=18_200))
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.get(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada)).json()["aposta"]

    assert corpo["retorno_centavos"] == 18_200
    assert corpo["lucro_centavos"] == 8_200
    assert corpo["data_aposta"] == "2026-09-10T21:00:00+00:00"
    assert corpo["criada_em"] == AGORA.isoformat()
    assert corpo["apagada"] is False


def test_a_settled_bet_can_be_settled_again(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(
        historico=[
            ("APOSTA_CRIADA", "ia", dict(CRIACAO)),
            ("RESULTADO_REGISTRADO", "manual", {"estado": "GREEN"}),
        ],
        aposta=_aposta(estado="GREEN", retorno_centavos=18_200),
    )
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "RED"}, headers=_cabecalho(privada)
    ).json()

    # A última palavra é da pessoa, como no caminho da casa: marcar errado tem conserto.
    assert corpo["aposta"]["estado"] == "RED"
    assert corpo["aposta"]["retorno_centavos"] == 0


def test_a_date_the_person_sends_keeps_its_hour(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.patch(
        f"/api/v1/apostas/{CHAVE}",
        json={"data_jogo": "2026-09-20T21:00:00"},
        headers=_cabecalho(privada),
    )

    # A mesma regra do trabalhador: data sem fuso é hora do Brasil. Com dois parsers, a aposta
    # andava três horas a cada correção.
    gravado = banco.gravados[-1]["data_jogo"]
    assert gravado.utcoffset() == timedelta(hours=-3)
    assert gravado.hour == 21


def test_repeating_a_result_does_not_erase_what_the_house_paid(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado",
        json={"estado": "GREEN", "retorno_centavos": 15_000},
        headers=_cabecalho(privada),
    )
    de_novo = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "GREEN"}, headers=_cabecalho(privada)
    ).json()

    # "Não falei disso" é manter: sem isto, repetir o mesmo resultado trocava o valor da casa pela
    # fórmula e mudava o dinheiro sem ninguém pedir.
    assert de_novo["aposta"]["retorno_centavos"] == 15_000


def test_changing_the_state_goes_back_to_the_formula(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado",
        json={"estado": "GREEN", "retorno_centavos": 15_000},
        headers=_cabecalho(privada),
    )
    mudou = cliente.post(
        f"/api/v1/apostas/{CHAVE}/resultado", json={"estado": "RED"}, headers=_cabecalho(privada)
    ).json()

    assert mudou["aposta"]["retorno_centavos"] == 0


def test_clearing_an_id_really_clears_it_on_the_bet(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(historico=[("APOSTA_CRIADA", "ia", dict(CRIACAO, tipster_id=3))])
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.patch(
        f"/api/v1/apostas/{CHAVE}", json={"tipster_id": None}, headers=_cabecalho(privada)
    )

    # Com `is not None`, a linha ficava com o tipster velho enquanto o histórico já dizia que saiu.
    assert "tipster_id" in banco.gravados[-1]
    assert banco.gravados[-1]["tipster_id"] is None


def test_correcting_the_words_of_a_bet_changes_what_the_person_sees(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco()
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.patch(
        f"/api/v1/apostas/{CHAVE}",
        json={"descricao": "Menos de 2.5", "casa": "KTO"},
        headers=_cabecalho(privada),
    )
    detalhe = cliente.get(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada)).json()

    # O detalhe lê o histórico dobrado: lendo só o evento de criação, a correção não aparecia.
    assert detalhe["selecoes"]["descricao"] == "Menos de 2.5"
    assert detalhe["selecoes"]["casa"] == "KTO"
    assert detalhe["aposta"]["descricao"] == "Menos de 2.5"


def test_deleting_a_bet_takes_its_review_off_the_queue(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(
        historico=[("APOSTA_CRIADA", "ia", dict(CRIACAO, revisao_motivo="a foto tem 2 cupons"))]
    )
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    cliente.delete(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada))

    # Quem apagou não tem nada a confirmar: a aposta sumiu da lista e a revisão sai junto.
    assert banco.resolvidas == [(CHAVE, None)]


def test_the_profit_agrees_with_the_stake_in_the_same_answer(monkeypatch, chave_rsa) -> None:
    privada, _ = chave_rsa
    banco = _banco(aposta=_aposta(estado="GREEN", retorno_centavos=18_200, stake_centavos=9_999))
    cliente = _cliente(monkeypatch, chave_rsa, banco)

    corpo = cliente.get(f"/api/v1/apostas/{CHAVE}", headers=_cabecalho(privada)).json()["aposta"]

    assert corpo["lucro_centavos"] == corpo["retorno_centavos"] - corpo["stake_centavos"]


def test_a_bet_created_without_an_account_says_it_will_not_show_in_the_house_filter(
    monkeypatch, chave_rsa
) -> None:
    privada, _ = chave_rsa
    banco = _banco()

    class SemConta:
        async def get_by_id(self, session, usuario_id, id_):
            return None

        async def get_vigente_by_nome_da_casa(self, session, usuario_id, nome, data):
            return None

    cliente = _cliente(monkeypatch, chave_rsa, banco)
    monkeypatch.setattr(rota, "ContaCasaRepo", SemConta)

    corpo = cliente.post(
        "/api/v1/apostas",
        json={"casa": "betano", "odd": 2.0, "stake_unidades": 1.0},
        headers=_cabecalho(privada),
    ).json()

    # O filtro por casa passa pelas contas: calado, o `casa_id` da resposta prometia o que não há.
    assert corpo["aposta"]["conta_casa_id"] is None
    assert corpo["aposta"]["conta_atribuicao"] == "UNASSIGNED"
    assert "não identificada" in corpo["aviso"]
