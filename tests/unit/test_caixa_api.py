from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bancaemdia import main
from bancaemdia.api.deps import get_current_user, get_current_user_snapshot
from bancaemdia.api.v1 import caixa as rota
from bancaemdia.db.session import get_db, get_db_snapshot
from bancaemdia.domain.caixa_service import saldo_da_conta
from bancaemdia.domain.registros import (
    Aposta,
    Banca,
    ContaCasa,
    Evento,
    LinhaExtrato,
    Movimento,
    MovimentoRequisicao,
    Usuario,
)

USUARIO = 7
AGORA = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
CABECALHO_IDEMPOTENTE = {"Idempotency-Key": "teste:unitario"}


def _conta(id_: int) -> ContaCasa:
    return ContaCasa(id_, USUARIO, id_ + 100, "principal", None, None, True)


def _movimento(**extra: Any) -> Movimento:
    campos = {
        "id": 1,
        "usuario_id": USUARIO,
        "conta_casa_id": 10,
        "tipo": "DEPOSITO",
        "valor_centavos": 50_000,
        "ocorrido_em": AGORA,
        "descricao": "começo",
        "transferencia_id": None,
    }
    return Movimento(**{**campos, **extra})


def _aposta(**extra: Any) -> Aposta:
    campos = {
        "id": 1,
        "usuario_id": USUARIO,
        "chave": "m:1",
        "chat_id": None,
        "message_id": None,
        "ordem_na_mensagem": 0,
        "midia_hash": None,
        "banca_id": None,
        "conta_casa_id": 10,
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
        "odd": 2.0,
        "retorno_centavos": 20_000,
        "estado": "GREEN",
        "origem": "manual",
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
    *,
    contas: list[ContaCasa] | None = None,
    movimentos: list[Movimento] | None = None,
    apostas: list[Aposta] | None = None,
    bancas: list[Banca] | None = None,
    trava_livre: bool = True,
    total: int | None = None,
):
    class Banco:
        def __init__(self) -> None:
            self.contas = list(contas if contas is not None else [_conta(10), _conta(20)])
            self.movimentos = list(movimentos or [])
            self.apostas = list(apostas or [])
            self.bancas = list(bancas or [])
            self.eventos: list[dict[str, object]] = []
            self.idempotencias: dict[tuple[int, str], MovimentoRequisicao] = {}
            self.sql: list[tuple[str, dict[str, object] | None]] = []
            self.travadas: list[int] = []
            self.filtros: dict[str, object] = {}
            self.filtros_extrato: dict[str, object] = {}
            self.paginacao = (0, 0)
            self.agregacoes: list[tuple[int, object]] = []
            self.commits = 0
            self.rollbacks = 0

    banco = Banco()

    class MovimentoRepo:
        async def append(self, session, dados):
            movimento = _movimento(id=len(banco.movimentos) + 1, **dados)
            banco.movimentos.append(movimento)
            return movimento

        async def list_page(self, session, usuario_id, filtros, pagina, tamanho):
            banco.filtros = filtros
            banco.paginacao = (pagina, tamanho)
            return list(banco.movimentos), len(banco.movimentos) if total is None else total

        async def list_by_usuario(self, session, usuario_id, desde=None, ate=None):
            return list(banco.movimentos)

        async def aggregate_saldos_by_usuario(self, session, usuario_id, data_corte=None):
            banco.agregacoes.append((usuario_id, data_corte))
            return {
                conta.id: saldo_da_conta(
                    conta.id,
                    banco.apostas,
                    banco.movimentos,
                    data_corte,
                )
                for conta in banco.contas
            }

    class ContaCasaRepo:
        async def get_many_for_update(self, session, usuario_id, ids):
            banco.travadas = list(ids)
            return [conta for conta in banco.contas if conta.id in ids]

        async def list_by_usuario(self, session, usuario_id):
            return list(banco.contas)

    class BancaRepo:
        async def list_by_usuario(self, session, usuario_id):
            return list(banco.bancas)

    class ExtratoRepo:
        async def list_page(self, session, usuario_id, filtros, pagina, tamanho):
            banco.filtros_extrato = filtros
            itens = [
                LinhaExtrato(
                    "movimento",
                    movimento.id,
                    movimento.conta_casa_id,
                    movimento.tipo,
                    movimento.ocorrido_em,
                    "ocorrido_em",
                    movimento.valor_centavos,
                    movimento.transferencia_id,
                    movimento.ocorrido_em,
                    movimento.descricao,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
                for movimento in banco.movimentos
            ]
            itens.extend(
                LinhaExtrato(
                    "aposta",
                    aposta.id,
                    aposta.conta_casa_id,
                    "APOSTA_LIQUIDADA",
                    aposta.data_aposta or aposta.criada_em,
                    "data_aposta" if aposta.data_aposta is not None else "criada_em",
                    None,
                    None,
                    None,
                    None,
                    aposta.chave,
                    aposta.estado,
                    aposta.stake_centavos,
                    aposta.retorno_centavos,
                    (
                        None
                        if aposta.retorno_centavos is None
                        else aposta.retorno_centavos - aposta.stake_centavos
                    ),
                    aposta.revisao_grave,
                )
                for aposta in banco.apostas
                if aposta.selecionada and aposta.estado != "PENDENTE"
            )
            conta_id = filtros.get("conta_casa_id")
            desde = filtros.get("desde")
            ate = filtros.get("ate")
            itens = [
                item
                for item in itens
                if (conta_id is None or item.conta_casa_id == conta_id)
                and (desde is None or item.data_referencia >= desde)
                and (ate is None or item.data_referencia < ate)
            ]
            itens.sort(key=lambda item: item.id, reverse=True)
            itens.sort(key=lambda item: item.origem)
            itens.sort(key=lambda item: item.data_referencia, reverse=True)
            inicio = (pagina - 1) * tamanho
            return itens[inicio : inicio + tamanho], len(itens)

    class EventoRepo:
        async def append(self, session, dados):
            banco.eventos.append(dados)
            return Evento(
                id=len(banco.eventos),
                usuario_id=USUARIO,
                tipo=str(dados["tipo"]),
                payload_json=dados["payload_json"],
                fonte=str(dados["fonte"]),
                confianca=None,
                chat_id=None,
                message_id=None,
                criado_em=AGORA,
                aposta_chave=None,
            )

    class MovimentoRequisicaoRepo:
        async def get(self, session, usuario_id, chave_idempotencia):
            return banco.idempotencias.get((usuario_id, chave_idempotencia))

        async def append(self, session, dados):
            registro = MovimentoRequisicao(
                id=len(banco.idempotencias) + 1,
                usuario_id=int(dados["usuario_id"]),
                chave_idempotencia=str(dados["chave_idempotencia"]),
                requisicao_hash=str(dados["requisicao_hash"]),
                resposta_json=cast(dict[str, Any], dados["resposta_json"]),
                criado_em=AGORA,
            )
            banco.idempotencias[registro.usuario_id, registro.chave_idempotencia] = registro
            return registro

    class Session:
        async def scalar(self, statement, params=None):
            banco.sql.append((str(statement), params))
            return trava_livre

        async def commit(self):
            banco.commits += 1

        async def rollback(self):
            banco.rollbacks += 1

    class Sessoes:
        async def abrir(self):
            yield Session()

    banco.repos = {
        "MovimentoRepo": MovimentoRepo,
        "ContaCasaRepo": ContaCasaRepo,
        "BancaRepo": BancaRepo,
        "ExtratoRepo": ExtratoRepo,
        "EventoRepo": EventoRepo,
        "MovimentoRequisicaoRepo": MovimentoRequisicaoRepo,
    }
    banco.sessao = Sessoes().abrir
    return banco


def _cliente(monkeypatch, banco) -> TestClient:
    def usuario() -> Usuario:
        return Usuario(USUARIO, "p@teste.local", "P", AGORA, True)

    for nome, classe in banco.repos.items():
        monkeypatch.setattr(rota, nome, classe)
    app = FastAPI()
    app.include_router(rota.router)
    app.dependency_overrides[get_current_user] = usuario
    app.dependency_overrides[get_current_user_snapshot] = usuario
    app.dependency_overrides[get_db] = banco.sessao
    app.dependency_overrides[get_db_snapshot] = banco.sessao
    return TestClient(app)


def test_transfer_is_two_signed_rows_one_event_and_one_commit(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post(
        "/api/v1/caixa",
        headers=CABECALHO_IDEMPOTENTE,
        json={
            "tipo": "transferencia",
            "valor_centavos": 12_500,
            "conta_casa_id": 20,
            "conta_casa_destino_id": 10,
            "ocorrido_em": "2026-09-21T09:00:00-03:00",
            "descricao": "Betano para bet365",
        },
    )

    corpo = resposta.json()
    assert resposta.status_code == 201
    assert [(m["conta_casa_id"], m["valor_centavos"]) for m in corpo["movimentos"]] == [
        (20, -12_500),
        (10, 12_500),
    ]
    assert corpo["transferencia_id"]
    assert {str(m.transferencia_id) for m in banco.movimentos} == {corpo["transferencia_id"]}
    assert banco.travadas == [10, 20]
    assert [chamada[1]["chave"] for chamada in banco.sql] == [
        f"caixa:idempotencia:{USUARIO}:teste:unitario",
        f"caixa:{USUARIO}:10",
        f"caixa:{USUARIO}:20",
    ]
    assert "pg_advisory_xact_lock" in banco.sql[0][0]
    assert "pg_try_advisory_xact_lock" not in banco.sql[0][0]
    assert banco.eventos[0]["tipo"] == "MOVIMENTO_REGISTRADO"
    assert len(banco.eventos[0]["payload_json"]["lancamentos"]) == 2
    assert len(banco.idempotencias) == 1
    assert (banco.commits, banco.rollbacks) == (1, 0)


def test_same_idempotency_key_replays_one_transfer_and_the_exact_response(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    pedido = {
        "tipo": "transferencia",
        "valor_centavos": 12_500,
        "conta_casa_id": 20,
        "conta_casa_destino_id": 10,
        "ocorrido_em": "2026-09-21T09:00:00-03:00",
    }
    cabecalho = {"Idempotency-Key": "transferencia:cliente:123"}

    primeira = cliente.post("/api/v1/caixa", json=pedido, headers=cabecalho)
    repetida = cliente.post("/api/v1/caixa", json=pedido, headers=cabecalho)

    assert (primeira.status_code, repetida.status_code) == (201, 201)
    assert repetida.json() == primeira.json()
    assert len(banco.movimentos) == 2
    assert len(banco.eventos) == 1
    assert len(banco.idempotencias) == 1
    assert (banco.commits, banco.rollbacks) == (1, 1)


def test_reusing_an_idempotency_key_for_another_request_is_a_conflict(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    cabecalho = {"Idempotency-Key": "deposito:cliente:123"}
    pedido = {
        "tipo": "deposito",
        "valor_centavos": 10_000,
        "conta_casa_id": 10,
        "ocorrido_em": AGORA.isoformat(),
    }

    primeira = cliente.post("/api/v1/caixa", json=pedido, headers=cabecalho)
    conflito = cliente.post(
        "/api/v1/caixa",
        json={**pedido, "valor_centavos": 20_000},
        headers=cabecalho,
    )

    assert primeira.status_code == 201
    assert conflito.status_code == 409
    assert conflito.json() == {"detail": rota.CHAVE_IDEMPOTENCIA_EM_CONFLITO}
    assert len(banco.movimentos) == 1
    assert len(banco.eventos) == 1
    assert len(banco.idempotencias) == 1
    assert (banco.commits, banco.rollbacks) == (1, 1)


def test_idempotency_key_is_required_for_every_financial_write(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post(
        "/api/v1/caixa",
        json={
            "tipo": "deposito",
            "valor_centavos": 10_000,
            "conta_casa_id": 10,
            "ocorrido_em": AGORA.isoformat(),
        },
    )

    assert resposta.status_code == 422
    assert banco.movimentos == []
    assert banco.eventos == []
    assert banco.sql == []


def test_a_busy_account_refuses_every_row_and_rolls_back(monkeypatch) -> None:
    banco = _banco(trava_livre=False)
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post(
        "/api/v1/caixa",
        headers=CABECALHO_IDEMPOTENTE,
        json={
            "tipo": "deposito",
            "valor_centavos": 10_000,
            "conta_casa_id": 10,
            "ocorrido_em": AGORA.isoformat(),
        },
    )

    assert resposta.status_code == 409
    assert banco.movimentos == []
    assert banco.eventos == []
    assert (banco.commits, banco.rollbacks) == (0, 1)


def test_an_invisible_account_is_not_found_and_nothing_is_written(monkeypatch) -> None:
    banco = _banco(contas=[])
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post(
        "/api/v1/caixa",
        headers=CABECALHO_IDEMPOTENTE,
        json={
            "tipo": "SAQUE",
            "valor_centavos": 10_000,
            "conta_casa_id": 99,
            "ocorrido_em": AGORA.isoformat(),
        },
    )

    assert resposta.status_code == 404
    assert banco.movimentos == []
    assert banco.eventos == []
    assert banco.rollbacks == 1


def test_an_invalid_movement_is_refused_before_taking_a_lock(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post(
        "/api/v1/caixa",
        headers=CABECALHO_IDEMPOTENTE,
        json={
            "tipo": "saque",
            "valor_centavos": -1,
            "conta_casa_id": 10,
            "ocorrido_em": AGORA.isoformat(),
        },
    )

    assert resposta.status_code == 422
    assert banco.sql == []
    assert banco.movimentos == []
    assert banco.rollbacks == 1


def test_boolean_money_and_ids_are_not_coerced_to_one(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.post(
        "/api/v1/caixa",
        headers=CABECALHO_IDEMPOTENTE,
        json={
            "tipo": "deposito",
            "valor_centavos": True,
            "conta_casa_id": True,
            "ocorrido_em": AGORA.isoformat(),
        },
    )

    assert resposta.status_code == 422
    assert banco.sql == []
    assert banco.movimentos == []


def test_cash_filters_refuse_ids_and_pages_that_do_not_fit_the_database(monkeypatch) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, banco)
    grande = 9_223_372_036_854_775_808

    respostas = [
        cliente.get(f"/api/v1/caixa?conta_casa_id={grande}"),
        cliente.get(f"/api/v1/caixa/extrato?conta_casa_id={grande}"),
        cliente.get(f"/api/v1/caixa?page={grande}"),
        cliente.get(f"/api/v1/caixa/extrato?page={grande}"),
    ]

    assert [resposta.status_code for resposta in respostas] == [422, 422, 422, 422]


def test_the_list_normalizes_the_type_and_keeps_the_page_contract(monkeypatch) -> None:
    banco = _banco(movimentos=[_movimento()], total=137)
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.get("/api/v1/caixa?tipo=deposito&conta_casa_id=10&page=2&page_size=25")

    corpo = resposta.json()
    assert corpo["pagination"] == {"page": 2, "page_size": 25, "total": 137}
    assert corpo["data"][0]["valor_centavos"] == 50_000
    assert banco.filtros["tipo"] == "DEPOSITO"
    assert banco.filtros["conta_casa_id"] == 10
    assert banco.paginacao == (2, 25)


def test_balance_reports_the_known_part_without_inventing_the_unknown_part(monkeypatch) -> None:
    banco = _banco(
        contas=[_conta(10), _conta(20)],
        movimentos=[
            _movimento(
                conta_casa_id=10,
                ocorrido_em=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            )
        ],
        apostas=[_aposta(estado="RED", retorno_centavos=0, conta_casa_id=10)],
        bancas=[Banca(3, USUARIO, "Principal", 100_000, AGORA)],
    )
    cliente = _cliente(monkeypatch, banco)

    corpo = cliente.get("/api/v1/caixa/saldo?data_corte=2026-09-21").json()

    assert corpo["saldo_conhecido_centavos"] == 40_000
    assert corpo["saldo_total_centavos"] is None
    assert corpo["contas_sem_saldo_confiavel"] == 1
    assert [conta["saldo_atual_centavos"] for conta in corpo["contas"]] == [40_000, None]
    assert corpo["saldo_total_escopo"] == "contas_casa"
    assert corpo["bancas"][0]["saldo_inicial_centavos"] == 100_000
    assert corpo["bancas"][0]["saldo_total_centavos"] is None
    assert "vínculo" in corpo["bancas"][0]["motivo_saldo_indisponivel"]


def test_balance_uses_the_bounded_database_aggregate(monkeypatch) -> None:
    banco = _banco(
        contas=[_conta(10), _conta(20)],
        movimentos=[_movimento(conta_casa_id=10), _movimento(id=2, conta_casa_id=20)],
        apostas=[_aposta(conta_casa_id=10), _aposta(id=2, chave="m:2", conta_casa_id=20)],
    )
    cliente = _cliente(monkeypatch, banco)

    resposta = cliente.get("/api/v1/caixa/saldo")

    assert resposta.status_code == 200
    assert banco.agregacoes == [(USUARIO, None)]
    assert [item["saldo_atual_centavos"] for item in resposta.json()["contas"]] == [
        50_000,
        50_000,
    ]


def test_statement_is_a_feed_and_does_not_invent_a_settlement_time_or_running_balance(
    monkeypatch,
) -> None:
    banco = _banco(
        movimentos=[_movimento()],
        apostas=[
            _aposta(),
            _aposta(id=2, chave="m:2", estado="PENDENTE", retorno_centavos=None),
            _aposta(id=3, chave="m:3", selecionada=False),
        ],
    )
    cliente = _cliente(monkeypatch, banco)

    corpo = cliente.get("/api/v1/caixa/extrato?conta_casa_id=10").json()

    assert corpo["pagination"]["total"] == 2
    assert [linha["origem"] for linha in corpo["data"]] == ["movimento", "aposta"]
    aposta = corpo["data"][1]
    assert aposta["resultado_liquido_centavos"] == 10_000
    assert aposta["data_referencia_origem"] == "data_aposta"
    assert aposta["liquidada_em"] is None
    assert all("saldo_apos_centavos" not in linha for linha in corpo["data"])


def test_the_router_is_part_of_the_application() -> None:
    caminhos = main.app.openapi()["paths"]

    assert "/api/v1/caixa" in caminhos
    assert "/api/v1/caixa/saldo" in caminhos
    assert "/api/v1/caixa/extrato" in caminhos


def test_openapi_describes_the_cash_contract_and_expected_errors() -> None:
    documento = main.app.openapi()
    caminhos = documento["paths"]
    post = caminhos["/api/v1/caixa"]["post"]

    assert set(post["responses"]) >= {"201", "404", "409", "422"}
    idempotencia = next(
        parametro for parametro in post["parameters"] if parametro["name"] == "Idempotency-Key"
    )
    assert idempotencia["in"] == "header"
    assert idempotencia["required"] is True
    assert post["responses"]["201"]["content"]["application/json"]["schema"]
    tipo = documento["components"]["schemas"]["MovimentoNovo"]["properties"]["tipo"]
    assert set(tipo["enum"]) == {"DEPOSITO", "SAQUE", "TRANSFERENCIA", "AJUSTE"}
    regras = documento["components"]["schemas"]["MovimentoNovo"]["allOf"]
    assert any(regra.get("then", {}).get("required") == ["conta_casa_id"] for regra in regras)
    assert any(
        regra.get("then", {}).get("required") == ["conta_casa_id", "conta_casa_destino_id"]
        for regra in regras
    )
    for caminho in ("/api/v1/caixa", "/api/v1/caixa/saldo", "/api/v1/caixa/extrato"):
        resposta = caminhos[caminho]["get"]["responses"]["200"]
        assert resposta["content"]["application/json"]["schema"]
