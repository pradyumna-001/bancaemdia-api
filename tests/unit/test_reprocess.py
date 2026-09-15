from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from kombu.exceptions import OperationalError
from structlog.testing import capture_logs

from bancaemdia.cli import reprocess
from bancaemdia.coleta.leitura import ColetaInvalidaError
from bancaemdia.domain.registros import ApostasPorOrigem
from bancaemdia.extracao import precos
from bancaemdia.extracao.cliente import VERSAO_PROMPT

BRASIL = timezone(timedelta(hours=-3))
SET_CONFIG = "SELECT set_config('app.current_user_id', :uid, true)"
RECEBIDA = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _bilhete(id_: str, *, resultado: str | None = "Lose", ganho: float = 0.0) -> dict[str, object]:
    bruto: dict[str, object] = {
        "id": id_,
        "bonusType": 0,
        "totalAmount": 160.0,
        "totalAmountWithCurrency": {"amount": 160.0, "currencyCode": "BRL"},
        "totalOdds": 1.9,
        "finalWinnings": ganho,
        "placedAt": 1785708161930,
        "finalBetResult": resultado,
        "settledAt": 1785716993030,
        "legs": [
            {
                "legItems": [
                    {
                        "eventId": "86389413",
                        "eventName": "Internacional - Corinthians",
                        "startTime": 1785709800000,
                        "selections": [{"description": "Mais de 41.5", "odds": 1.9}],
                    }
                ]
            }
        ],
    }
    if resultado is None:
        del bruto["finalBetResult"], bruto["settledAt"]
    return bruto


def _coleta(id_, bruto, *, usuario_id=7, casa_id=1, horas=0):
    return SimpleNamespace(
        id=id_,
        usuario_id=usuario_id,
        casa_id=casa_id,
        bruto_json=bruto,
        recebido_em=RECEBIDA + timedelta(hours=horas),
    )


def _banco(contagens=None, usuarios=(), coletas=()):
    class Banco:
        def __init__(self):
            self.contagens = contagens or {}
            self.usuarios = list(usuarios)
            self.coletas = {coleta.id: coleta for coleta in coletas}
            self.casas = {1: "Betano", 2: "KTO", 3: "bet365"}
            self.sql = []
            self.contadas = []
            self.enfileiradas = []
            self.inexistentes = set()
            self.descartado = False

    banco = Banco()

    class ApostaRepo:
        async def count_by_origem(self, session, usuario_id, desde=None, ate=None):
            banco.contadas.append((usuario_id, desde, ate))
            return banco.contagens.get(usuario_id, [])

    class UsuarioRepo:
        async def get_by_id(self, session, id_):
            return None if id_ in banco.inexistentes else SimpleNamespace(id=id_, ativo=True)

        async def list_active_ids(self, session, depois_de=0, limite=1000):
            return [u for u in banco.usuarios if u > depois_de][:limite]

    class ColetaCasaRepo:
        async def get_by_id(self, session, usuario_id, id_):
            coleta = banco.coletas.get(id_)
            return coleta if coleta is not None and coleta.usuario_id == usuario_id else None

        async def list_by_casa(self, session, usuario_id, casa_id, depois_de=0, limite=1000):
            linhas = sorted(
                (
                    coleta
                    for coleta in banco.coletas.values()
                    if (coleta.usuario_id, coleta.casa_id) == (usuario_id, casa_id)
                    and coleta.id > depois_de
                ),
                key=lambda coleta: coleta.id,
            )
            return linhas[:limite]

    class CasaRepo:
        async def get_nome_by_id(self, session, casa_id):
            return banco.casas.get(casa_id)

        async def get_id_by_nome(self, session, nome):
            return next((id_ for id_, casa in banco.casas.items() if casa == nome), None)

    class Session:
        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement, params=None):
            banco.sql.append((str(statement), params))

    class Engine:
        async def dispose(self):
            banco.descartado = True

    banco.repos = {
        "ApostaRepo": ApostaRepo,
        "UsuarioRepo": UsuarioRepo,
        "ColetaCasaRepo": ColetaCasaRepo,
        "CasaRepo": CasaRepo,
    }
    banco.session = Session
    banco.engine = Engine()
    return banco


def _instalar(monkeypatch, banco):
    for nome, classe in banco.repos.items():
        monkeypatch.setattr(reprocess, nome, classe)
    monkeypatch.setattr(reprocess, "AsyncSession", lambda *a, **k: banco.session())
    monkeypatch.setattr(reprocess, "get_engine", lambda: banco.engine)
    monkeypatch.setattr(
        reprocess,
        "_enfileirar",
        lambda usuario_id, coleta_ids: banco.enfileiradas.append((usuario_id, coleta_ids)),
    )


def test_price_is_the_reference_measured_in_the_clean_bank() -> None:
    assert precos.USD_POR_BILHETE_REFERENCIA == pytest.approx(0.0054)
    assert precos.ORIGEM_DA_REFERENCIA == "referência medida em 279 bilhetes limpos"
    assert precos.em_reais(100 * precos.USD_POR_BILHETE_REFERENCIA) == pytest.approx(2.92, abs=0.01)


async def test_user_plan_prices_only_messages_flagged_for_review_by_default(monkeypatch) -> None:
    banco = _banco(
        contagens={
            7: [ApostasPorOrigem("casa", 4, 1, 4, 1), ApostasPorOrigem("telegram", 10, 3, 8, 2)]
        }
    )
    _instalar(monkeypatch, banco)

    plano = await reprocess.reprocessar_usuario(banco.engine, 7)

    assert (plano.usuarios, plano.apostas, plano.bilhetes, plano.fora_do_escopo) == (1, 3, 2, 7)
    assert plano.sem_releitura == {"casa": 4}
    assert plano.custo_usd == pytest.approx(2 * precos.USD_POR_BILHETE_REFERENCIA)
    assert (SET_CONFIG, {"uid": "7"}) in banco.sql


async def test_everything_scope_prices_every_telegram_message(monkeypatch) -> None:
    banco = _banco(contagens={7: [ApostasPorOrigem("telegram", 10, 3, 8, 2)]})
    _instalar(monkeypatch, banco)

    plano = await reprocess.reprocessar_usuario(banco.engine, 7, escopo=reprocess.ESCOPO_TUDO)

    assert (plano.apostas, plano.bilhetes, plano.fora_do_escopo) == (10, 8, 0)
    assert plano.custo_usd == pytest.approx(8 * precos.USD_POR_BILHETE_REFERENCIA)


@pytest.mark.parametrize("versao", ["extrair_bilhete_v4", "outro_prompt"])
async def test_prompt_version_other_than_the_published_one_is_refused(monkeypatch, versao) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    with pytest.raises(reprocess.VersaoDoPromptError, match="publique o prompt novo"):
        await reprocess.reprocessar_usuario(banco.engine, 7, versao_prompt=versao)
    with pytest.raises(reprocess.VersaoDoPromptError):
        await reprocess.reler_todas(banco.engine, versao)

    assert banco.contadas == []


async def test_published_prompt_version_is_accepted(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    await reprocess.reprocessar_usuario(banco.engine, 7, versao_prompt=VERSAO_PROMPT)

    assert banco.contadas == [(7, None, None)]


async def test_full_releitura_counts_every_active_user_page_by_page(monkeypatch) -> None:
    usuarios = (1, 2, 5)
    banco = _banco(
        contagens={u: [ApostasPorOrigem("telegram", 2, 1, 1, 1)] for u in usuarios},
        usuarios=usuarios,
    )
    _instalar(monkeypatch, banco)
    monkeypatch.setattr(reprocess, "PAGINA", 2)
    monkeypatch.setattr(reprocess, "LOTE", 2)

    with capture_logs() as logs:
        plano = await reprocess.reler_todas(banco.engine, VERSAO_PROMPT)

    assert [contada[0] for contada in banco.contadas] == [1, 2, 5]
    assert (plano.usuarios, plano.apostas, plano.bilhetes) == (3, 3, 3)
    assert [log["usuarios"] for log in logs if log["event"] == "reler_todas_progresso"] == [2]


async def test_one_row_is_only_shown_until_sim_is_given(monkeypatch) -> None:
    banco = _banco(coletas=[_coleta(11, _bilhete("A"))])
    _instalar(monkeypatch, banco)

    mostrado = await reprocess.reprocessar_coleta(banco.engine, 7, 11)
    autorizado = await reprocess.reprocessar_coleta(banco.engine, 7, 11, sim=True)

    assert (mostrado.casa, mostrado.apostas, mostrado.enfileiradas) == ("betano", [[11]], 0)
    assert (autorizado.enfileiradas, banco.enfileiradas) == (1, [(7, [11])])
    assert (SET_CONFIG, {"uid": "7"}) in banco.sql


async def test_row_sends_the_whole_history_of_its_bet(monkeypatch) -> None:
    banco = _banco(
        coletas=[
            _coleta(11, _bilhete("A", resultado=None), horas=1),
            _coleta(12, _bilhete("B"), horas=2),
            _coleta(14, _bilhete("A", resultado="Win", ganho=304.0), horas=3),
        ]
    )
    _instalar(monkeypatch, banco)

    reprocesso = await reprocess.reprocessar_coleta(banco.engine, 7, 11, sim=True)

    assert (reprocesso.pedida, reprocesso.apostas, reprocesso.guardadas) == (11, [[11, 14]], 2)
    assert banco.enfileiradas == [(7, [11, 14])]


@pytest.mark.parametrize("coleta_id", [99, 13])
async def test_row_that_is_not_this_users_is_refused(monkeypatch, coleta_id) -> None:
    banco = _banco(coletas=[_coleta(11, _bilhete("A")), _coleta(13, _bilhete("B"), usuario_id=8)])
    _instalar(monkeypatch, banco)

    with pytest.raises(reprocess.ColetaNaoEncontradaError, match=f"não achei a coleta {coleta_id}"):
        await reprocess.reprocessar_coleta(banco.engine, 7, coleta_id, sim=True)

    assert banco.enfileiradas == []


@pytest.mark.parametrize(
    ("coleta", "motivo"),
    [
        (_coleta(12, {"ticket": 1}, casa_id=3), "ainda não sei ler o histórico da bet365"),
        (_coleta(12, {"ticket": 1}), "sem identificador"),
    ],
)
async def test_row_without_a_reader_or_that_does_not_read_is_refused(
    monkeypatch, coleta, motivo
) -> None:
    banco = _banco(coletas=[coleta])
    _instalar(monkeypatch, banco)

    with pytest.raises(ColetaInvalidaError, match=motivo):
        await reprocess.reprocessar_coleta(banco.engine, 7, 12, sim=True)

    assert banco.enfileiradas == []


async def test_house_sends_each_bet_once_with_its_captures_in_order(monkeypatch) -> None:
    banco = _banco(
        coletas=[
            _coleta(1, _bilhete("A", resultado=None), horas=1),
            _coleta(2, _bilhete("A", resultado="Win", ganho=304.0), horas=3),
            _coleta(3, _bilhete("B"), horas=1),
            _coleta(4, {"ticket": 1}, horas=5),
            _coleta(5, _bilhete("B", ganho=0.5), horas=0),
            _coleta(6, _bilhete("C"), usuario_id=8),
        ]
    )
    _instalar(monkeypatch, banco)

    reprocesso = await reprocess.reprocessar_casa(banco.engine, 7, " Betano ", sim=True)

    assert (reprocesso.casa, reprocesso.guardadas, reprocesso.ilegiveis) == ("betano", 5, 1)
    assert reprocesso.apostas == [[1, 2], [5, 3]]
    assert banco.enfileiradas == [(7, [1, 2]), (7, [5, 3])]


async def test_house_rows_are_read_and_queued_page_by_page_as_they_were_shown(monkeypatch) -> None:
    banco = _banco(coletas=[_coleta(id_, _bilhete(f"B{id_}")) for id_ in (3, 4, 8, 9, 10)])
    _instalar(monkeypatch, banco)
    monkeypatch.setattr(reprocess, "PAGINA", 2)
    monkeypatch.setattr(reprocess, "LOTE", 2)

    mostrado = await reprocess.reprocessar_casa(banco.engine, 7, "betano")
    with capture_logs() as logs:
        autorizado = await reprocess.reprocessar_casa(banco.engine, 7, "betano", sim=True)

    assert (mostrado.apostas, mostrado.enfileiradas) == ([[3], [4], [8], [9], [10]], 0)
    assert banco.enfileiradas == [(7, historico) for historico in mostrado.apostas]
    assert autorizado.enfileiradas == len(mostrado.apostas)
    progresso = [
        log["enfileiradas"] for log in logs if log["event"] == "reprocessar_coleta_progresso"
    ]
    assert progresso == [2, 4]


@pytest.mark.parametrize(
    ("casa", "motivo"),
    [("bet365", "ainda não sei ler o histórico da bet365"), ("bet ano", "envio quebrado")],
)
async def test_house_without_a_reader_or_with_a_broken_name_is_refused(
    monkeypatch, casa, motivo
) -> None:
    banco = _banco(coletas=[_coleta(12, {"ticket": 1}, casa_id=3)])
    _instalar(monkeypatch, banco)

    with pytest.raises(ColetaInvalidaError, match=motivo):
        await reprocess.reprocessar_casa(banco.engine, 7, casa, sim=True)

    assert (banco.sql, banco.enfileiradas) == ([], [])


def test_bet_history_is_queued_to_the_task_that_applies_it_in_order(monkeypatch) -> None:
    pedidos = []
    monkeypatch.setattr(
        reprocess.materializar_coletas_task, "apply_async", lambda **kwargs: pedidos.append(kwargs)
    )

    reprocess._enfileirar(7, [11, 14])

    assert reprocess.materializar_coletas_task.name == "materialization.materializar_coletas"
    assert pedidos == [{"kwargs": {"usuario_id": 7, "coleta_ids": [11, 14]}}]


def test_user_command_counts_whole_days_and_says_why_nothing_was_sent(monkeypatch) -> None:
    banco = _banco(contagens={7: [ApostasPorOrigem("telegram", 3, 1, 2, 1)]})
    _instalar(monkeypatch, banco)
    argv = ["usuario", "--usuario-id", "7", "--desde", "2026-08-01", "--ate", "2026-08-31"]

    with capture_logs() as logs:
        codigo = reprocess.main([*argv, "--tudo", "--sim"])

    (plano,) = [log for log in logs if log["event"] == "reprocessar_usuario"]
    assert codigo == 0
    assert banco.contadas == [
        (7, datetime(2026, 8, 1, tzinfo=BRASIL), datetime(2026, 9, 1, tzinfo=BRASIL))
    ]
    assert (plano["escopo"], plano["bilhetes_para_a_ia"], plano["custo_estimado_usd"]) == (
        "tudo",
        2,
        0.01,
    )
    assert plano["preco_por_bilhete"] == precos.ORIGEM_DA_REFERENCIA
    assert "nada foi enviado para a IA" in plano["recado"]
    assert banco.descartado


def test_full_releitura_command_needs_a_prompt_version() -> None:
    with pytest.raises(SystemExit) as saida:
        reprocess.analisador().parse_args(["reler-todas"])

    assert saida.value.code == 2


@pytest.mark.parametrize(
    ("argv", "enfileiradas", "recado"),
    [
        (["--casa", "betano"], [], reprocess.SEM_SIM),
        (["--casa", "betano", "--dry-run"], [], reprocess.SEM_SIM),
        (["--casa", "betano", "--sim"], [(7, [11])], reprocess.CONFIRA),
        (["--casa", "kto", "--sim"], [], reprocess.NADA_GUARDADO),
        (["--casa", "superbet", "--sim"], [], reprocess.NADA_GUARDADO),
    ],
)
def test_collection_command_only_queues_with_sim(monkeypatch, argv, enfileiradas, recado) -> None:
    banco = _banco(coletas=[_coleta(11, _bilhete("A"))])
    _instalar(monkeypatch, banco)

    with capture_logs() as logs:
        codigo = reprocess.main(["coleta", "--usuario-id", "7", *argv])

    (resumo,) = [log for log in logs if log["event"] == "reprocessar_coleta"]
    assert codigo == 0
    assert banco.enfileiradas == enfileiradas
    assert (resumo["recado"], resumo["custo_estimado_brl"]) == (recado, 0.0)


def test_collection_command_says_when_nothing_stored_reads_and_how_many_captures_go(
    monkeypatch,
) -> None:
    banco = _banco(
        coletas=[
            _coleta(11, _bilhete("A", resultado=None), horas=1),
            _coleta(14, _bilhete("A"), horas=2),
            _coleta(20, {"ticket": 1}, casa_id=2),
        ]
    )
    _instalar(monkeypatch, banco)

    with capture_logs() as logs:
        reprocess.main(["coleta", "--usuario-id", "7", "--coleta-id", "11"])
        reprocess.main(["coleta", "--usuario-id", "7", "--casa", "kto"])

    pedida, ilegivel = [log for log in logs if log["event"] == "reprocessar_coleta"]
    assert (pedida["pedida"], pedida["apostas"], pedida["capturas"]) == (11, 1, 2)
    assert pedida["recado"] == reprocess.SEM_SIM
    assert (ilegivel["guardadas"], ilegivel["ilegiveis"]) == (1, 1)
    assert ilegivel["recado"] == reprocess.NADA_LEGIVEL


def test_refusal_is_logged_with_its_reason_and_exits_with_an_error(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    with capture_logs() as logs:
        codigo = reprocess.main(["coleta", "--usuario-id", "7", "--casa", "bet365", "--sim"])

    (recusa,) = logs
    assert codigo == 1
    assert (recusa["event"], recusa["log_level"]) == ("reprocessamento_recusado", "error")
    assert recusa["recado"].startswith("ainda não sei ler o histórico da bet365")
    assert banco.descartado


def test_user_that_does_not_exist_is_refused_before_anything_is_counted(monkeypatch) -> None:
    banco = _banco(coletas=[_coleta(11, _bilhete("A"), usuario_id=99)])
    banco.inexistentes.add(99)
    _instalar(monkeypatch, banco)

    with capture_logs() as logs:
        codigos = [
            reprocess.main(["usuario", "--usuario-id", "99"]),
            reprocess.main(["coleta", "--usuario-id", "99", "--casa", "betano", "--sim"]),
            reprocess.main(["coleta", "--usuario-id", "99", "--coleta-id", "11", "--sim"]),
        ]

    assert codigos == [1, 1, 1]
    assert [log["recado"] for log in logs] == ["o usuário 99 não existe"] * 3
    assert (banco.contadas, banco.enfileiradas) == ([], [])


def test_queue_dropping_midway_says_how_many_went_and_that_running_again_is_safe(
    monkeypatch,
) -> None:
    banco = _banco(coletas=[_coleta(id_, _bilhete(f"B{id_}")) for id_ in (3, 4, 8)])
    _instalar(monkeypatch, banco)

    def cai_na_segunda(usuario_id, coleta_ids):
        if banco.enfileiradas:
            raise OperationalError("broker down")
        banco.enfileiradas.append((usuario_id, coleta_ids))

    monkeypatch.setattr(reprocess, "_enfileirar", cai_na_segunda)

    with capture_logs() as logs:
        codigo = reprocess.main(["coleta", "--usuario-id", "7", "--casa", "betano", "--sim"])

    (recusa,) = logs
    assert codigo == 1
    assert recusa["recado"].startswith("a fila caiu depois de 1 de 3 apostas")
    assert "sem duplicar nada" in recusa["recado"]
    assert banco.enfileiradas == [(7, [3])]


def test_dry_run_and_sim_cannot_be_given_together() -> None:
    with pytest.raises(SystemExit) as saida:
        reprocess.analisador().parse_args([
            "coleta",
            "--usuario-id",
            "7",
            "--casa",
            "betano",
            "--dry-run",
            "--sim",
        ])

    assert saida.value.code == 2
