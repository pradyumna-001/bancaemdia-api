from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg.exceptions
import pytest
from celery.utils.time import get_exponential_backoff_interval
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from bancaemdia.config import get_settings
from bancaemdia.domain import materializar
from bancaemdia.domain.coleta_casa import ApostaInvalidaError
from bancaemdia.domain.conferencias import GRAVES, Origem, conferir
from bancaemdia.domain.event_bus import ApostaCriada
from bancaemdia.extracao import rodada
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao
from bancaemdia.workers import celery_app, materialization

BRASIL = timedelta(hours=-3)


def _bom(**campos):
    return ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    ).model_copy(update=campos)


def _extracao(**campos):
    return {
        "usuario_id": 7,
        "chat_id": 100,
        "message_id": 200,
        "postada_em": "2026-07-24T16:00:00",
        "versao_prompt": "extrair_bilhete_v3",
        "bilhete": _bom().model_dump(mode="json"),
        "motivo": None,
        "grave": False,
        "cupons": [],
        "degrau": "BARATO",
        "custo_usd": 0.012,
        "nao_e_aposta": False,
        **campos,
    }


def _banco(unidade=None, contas=(None,), desatualizada=False):
    class Registro:
        def __init__(self, dados):
            self.tipo = dados.get("tipo")
            self.fonte = dados.get("fonte")
            self.payload_json = dados.get("payload_json")
            self.valor_centavos = dados.get("valor_centavos")

    class Banco:
        def __init__(self):
            self.eventos = []
            self.upserts = []
            self.revisoes = []
            self.sql = []
            self.contas = list(contas)
            self.contas_buscadas = []
            self.publicados = []

    banco = Banco()

    class EventoRepo:
        async def list_by_aposta_chave(self, session, usuario_id, chave):
            return [
                Registro(d)
                for d in banco.eventos
                if (d["usuario_id"], d["aposta_chave"]) == (usuario_id, chave)
            ]

        async def append(self, session, dados):
            banco.eventos.append(dados)
            return Registro(dados)

    class ApostaRepo:
        async def upsert_materializada(self, session, dados):
            banco.upserts.append(dados)
            return None if desatualizada else dados

    class ContaCasaRepo:
        async def get_vigente_by_nome_da_casa(self, session, usuario_id, nome, data=None):
            banco.contas_buscadas.append((nome, data))
            return banco.contas.pop(0) if len(banco.contas) > 1 else banco.contas[0]

    class RevisaoPendenteRepo:
        async def resolve_superseded(self, session, usuario_id, aposta_chave, motivo):
            vencidas = [
                r
                for r in banco.revisoes
                if r["extracao_bruta"]["aposta_chave"] == aposta_chave
                and not r["resolvida"]
                and r["motivo"] != motivo
            ]
            for revisao in vencidas:
                revisao["resolvida"] = True
            return len(vencidas)

        async def has_open(self, session, usuario_id, aposta_chave, motivo):
            return any(
                (r["extracao_bruta"]["aposta_chave"], r["motivo"], r["resolvida"])
                == (aposta_chave, motivo, False)
                for r in banco.revisoes
            )

        async def create(self, session, dados):
            banco.revisoes.append({**dados, "resolvida": False})
            return dados

    class UnidadeRepo:
        async def get_vigente(self, session, usuario_id, data):
            return None if unidade is None else Registro({"valor_centavos": unidade})

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
        def connect(self):
            return Session()

    class Bus:
        def publish(self, evento):
            banco.publicados.append(evento)

    banco.repos = {
        "EventoRepo": EventoRepo,
        "ApostaRepo": ApostaRepo,
        "ContaCasaRepo": ContaCasaRepo,
        "RevisaoPendenteRepo": RevisaoPendenteRepo,
        "UnidadeRepo": UnidadeRepo,
    }
    banco.session = Session()
    banco.engine = Engine()
    banco.bus = Bus()
    return banco


def _instalar(monkeypatch, banco):
    for nome, classe in banco.repos.items():
        monkeypatch.setattr(materialization, nome, classe)
    monkeypatch.setattr(materialization, "AsyncSession", lambda *a, **k: banco.session)
    monkeypatch.setattr(materialization, "get_engine", lambda: banco.engine)
    monkeypatch.setattr(materialization, "get_event_bus", lambda: banco.bus)


def _conta(id_):
    return SimpleNamespace(id=id_)


def _falhas(motivo):
    return REGISTRY.get_sample_value("materialization_failed_total", {"reason": motivo}) or 0.0


def _abertas(banco):
    return [r["motivo"] for r in banco.revisoes if not r["resolvida"]]


def test_task_is_routed_to_the_materialization_queue() -> None:
    task = materialization.materializar_aposta_task

    assert task.name == "materialization.materializar_aposta"
    assert celery_app.app.amqp.router.route({}, task.name)["queue"].name == "materialization"
    assert "bancaemdia.workers.materialization" in celery_app.app.conf.include


def test_task_retries_database_failures_three_times() -> None:
    task = materialization.materializar_aposta_task

    esperas = [
        get_exponential_backoff_interval(
            factor=int(task.retry_backoff),
            retries=tentativa,
            maximum=task.retry_backoff_max,
            full_jitter=task.retry_jitter,
        )
        for tentativa in range(task.max_retries)
    ]

    assert set(task.autoretry_for) == set(materialization.RETRY_ON)
    assert asyncpg.exceptions.TooManyConnectionsError in task.autoretry_for
    assert asyncpg.exceptions.CannotConnectNowError in task.autoretry_for
    assert ValidationError not in task.autoretry_for
    assert esperas == [30, 60, 120]


def test_new_reading_creates_the_bet_its_event_and_publishes_it(monkeypatch) -> None:
    banco = _banco(unidade=5_000, contas=[_conta(3)])
    _instalar(monkeypatch, banco)

    resultado = materialization.materializar_aposta(7, _extracao(), "hash-da-foto")

    assert resultado == {
        "usuario_id": 7,
        "apostas": ["t:100:200:0"],
        "criadas": 1,
        "eventos": 1,
        "revisoes": 0,
    }
    (evento,) = banco.eventos
    assert (evento["tipo"], evento["fonte"], evento["aposta_chave"]) == (
        "APOSTA_CRIADA",
        "ia",
        "t:100:200:0",
    )
    assert (evento["chat_id"], evento["message_id"]) == (100, 200)
    assert evento["payload_json"]["valor_unidade_centavos"] == 5_000
    (aposta,) = banco.upserts
    postada = datetime(2026, 7, 24, 19, tzinfo=UTC)
    assert (aposta["conta_casa_id"], aposta["midia_hash"], aposta["data_aposta"]) == (
        3,
        "hash-da-foto",
        postada,
    )
    assert aposta["data_aposta"].utcoffset() == BRASIL
    assert (aposta["stake_centavos"], aposta["origem"], aposta["revisao_grave"]) == (
        0,
        "telegram",
        False,
    )
    assert banco.contas_buscadas == [("Betano", postada)]
    assert banco.publicados == [ApostaCriada(7, "t:100:200:0", "telegram", False)]
    assert any("set_config('app.current_user_id'" in sql for sql, _ in banco.sql)
    assert (
        "SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))",
        {"chave": "7:t:100:200:0"},
    ) in banco.sql


def test_resend_touches_the_bet_without_creating_or_publishing_again(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    materialization.materializar_aposta(7, _extracao(), "hash-da-foto")
    segunda = materialization.materializar_aposta(7, _extracao(), "hash-da-foto")

    assert (segunda["criadas"], segunda["eventos"]) == (0, 0)
    assert [e["tipo"] for e in banco.eventos] == ["APOSTA_CRIADA"]
    assert len(banco.publicados) == 1
    assert len(banco.upserts) == 2


def test_resend_keeps_the_account_and_the_image_it_cannot_see(monkeypatch) -> None:
    banco = _banco(contas=[_conta(3), None])
    _instalar(monkeypatch, banco)

    materialization.materializar_aposta(7, _extracao(), "hash-da-foto")
    materialization.materializar_aposta(7, _extracao(), None)

    primeira, segunda = banco.upserts
    assert (primeira["conta_casa_id"], primeira["midia_hash"]) == (3, "hash-da-foto")
    assert "conta_casa_id" not in segunda
    assert "midia_hash" not in segunda


def test_better_reading_is_recorded_as_a_program_correction(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    falhou = _extracao(bilhete=None, motivo="a leitura falhou (APIError)", grave=True)

    materialization.materializar_aposta(7, falhou, "hash-da-foto")
    materialization.materializar_aposta(7, _extracao(), "hash-da-foto")

    assert [(e["tipo"], e["fonte"]) for e in banco.eventos] == [
        ("APOSTA_CRIADA", "ia"),
        ("CORRECAO_MANUAL", "ia"),
    ]
    aposta = banco.upserts[-1]
    assert aposta["odd"] == pytest.approx(1.82)
    assert aposta["revisao_grave"] is False
    assert aposta["conta_casa_id"] is None
    assert _abertas(banco) == []


def test_grave_reading_opens_one_review_even_when_sent_twice(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    grave = _extracao(motivo="coerência das odds: diverge", grave=True)

    primeira = materialization.materializar_aposta(7, grave, "hash-da-foto")
    materialization.materializar_aposta(7, grave, "hash-da-foto")

    assert primeira["revisoes"] == 1
    (revisao,) = banco.revisoes
    assert (revisao["motivo"], revisao["midia_hash"], revisao["resolvida"]) == (
        "coerência das odds: diverge",
        "hash-da-foto",
        False,
    )
    assert revisao["extracao_bruta"] == {"aposta_chave": "t:100:200:0", "extracao": grave}
    assert banco.publicados == [ApostaCriada(7, "t:100:200:0", "telegram", True)]


def test_new_grave_reason_replaces_the_open_review(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    materialization.materializar_aposta(7, _extracao(motivo="primeiro", grave=True), "h")
    materialization.materializar_aposta(7, _extracao(motivo="segundo", grave=True), "h")

    assert [r["motivo"] for r in banco.revisoes] == ["primeiro", "segundo"]
    assert _abertas(banco) == ["segundo"]


def test_grave_bet_without_a_reason_gets_the_default_one(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    materialization.materializar_aposta(7, _extracao(motivo=None, grave=True), "h")

    assert _abertas(banco) == [materialization.MOTIVO_GRAVE_PADRAO]


def test_multi_coupon_image_materializes_every_coupon(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    cupons = [
        {"bilhete": _bom().model_dump(mode="json"), "motivo": None, "grave": False},
        {"bilhete": _bom(odd_total=3.0).model_dump(mode="json"), "motivo": None, "grave": False},
    ]

    resultado = materialization.materializar_aposta(7, _extracao(cupons=cupons), "h")

    assert resultado["apostas"] == ["t:100:200:0", "t:100:200:1"]
    assert resultado["revisoes"] == 2


def test_reading_without_a_date_uses_the_default_unit_and_no_date(monkeypatch) -> None:
    banco = _banco(unidade=5_000)
    _instalar(monkeypatch, banco)

    materialization.materializar_aposta(7, _extracao(postada_em=None), "h")

    assert banco.eventos[0]["payload_json"]["valor_unidade_centavos"] == 10_000
    assert banco.upserts[0]["data_aposta"] is None


def test_losing_to_a_newer_write_rolls_back_and_retries(monkeypatch) -> None:
    banco = _banco(desatualizada=True)
    _instalar(monkeypatch, banco)
    falhas = _falhas("banco")

    with pytest.raises(materialization.GravacaoConcorrenteError):
        materialization.materializar_aposta(7, _extracao(), "h")

    assert _falhas("banco") == pytest.approx(falhas + 1)
    assert materialization.GravacaoConcorrenteError in materialization.RETRY_ON


@pytest.mark.parametrize(
    "extracao",
    [
        _extracao(cupons="nope"),
        _extracao(chat_id=None),
        _extracao(message_id=None),
        _extracao(postada_em="ontem às 16h"),
    ],
)
def test_invalid_payload_is_counted_and_not_retried(monkeypatch, extracao) -> None:
    _instalar(monkeypatch, _banco())
    falhas = _falhas("payload_invalido")

    with pytest.raises(ValidationError):
        materialization.materializar_aposta(7, extracao, "h")

    assert _falhas("payload_invalido") == pytest.approx(falhas + 1)


@pytest.mark.parametrize(
    ("erro", "motivo"),
    [
        (OperationalError("SELECT 1", {}, ConnectionError("recusada")), "banco"),
        (RuntimeError("inesperado"), "inesperado"),
    ],
)
def test_every_failure_is_counted_and_raised(monkeypatch, erro, motivo) -> None:
    class Banco:
        async def gravar(self, *args, **kwargs):
            raise erro

    monkeypatch.setattr(materialization, "get_engine", lambda: None)
    monkeypatch.setattr(materialization, "gravar_leitura", Banco().gravar)
    falhas = _falhas(motivo)

    with pytest.raises(type(erro)):
        materialization.materializar_aposta(7, _extracao(), "h")

    assert _falhas(motivo) == pytest.approx(falhas + 1)


def test_task_runs_eagerly(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)

    resultado = materialization.materializar_aposta_task.apply(
        kwargs={"usuario_id": 7, "extracao_json": _extracao(), "midia_hash": "h"}
    ).get()

    assert resultado["criadas"] == 1


def test_extraction_output_is_the_materialization_input() -> None:
    cupom = {"bilhete": _bom().model_dump(mode="json"), "motivo": "m", "grave": True}

    leitura = materialization.ExtracaoDoJson.model_validate(
        _extracao(cupons=[cupom, cupom])
    ).para_leitura()
    sem_bilhete = materialization.ExtracaoDoJson.model_validate(_extracao(bilhete=None))

    assert leitura.bilhete.odd_total == pytest.approx(1.82)
    assert [(c.motivo, c.grave) for c in leitura.cupons] == [("m", True), ("m", True)]
    assert sem_bilhete.para_leitura().bilhete is None


def test_post_date_without_a_timezone_is_brazil_time() -> None:
    sem_fuso = materialization._data("2026-07-24T16:00:00")
    com_fuso = materialization._data("2026-07-24T16:00:00+00:00")

    assert sem_fuso == datetime(2026, 7, 24, 19, tzinfo=UTC)
    assert sem_fuso is not None and sem_fuso.utcoffset() == BRASIL
    assert com_fuso == datetime(2026, 7, 24, 16, tzinfo=UTC)
    assert materialization._data(None) is None


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _multipla_acima_do_teto():
    return _bom(
        tipo="multipla",
        selecoes=[
            Selecao(mercado="Resultado", escolha="Velez", odd=1.5, evento="Velez x Instituto"),
            Selecao(mercado="Resultado", escolha="Boca", odd=2.0, evento="Boca x River"),
        ],
        odd_total=900.0,
    )


def test_review_opened_is_counted_once_by_its_first_grave_conference(monkeypatch) -> None:
    _instalar(monkeypatch, _banco())
    parecer = conferir(_multipla_acima_do_teto().para_bilhete(), origem=Origem.IA)
    grave = _extracao(motivo=parecer.motivo, grave=parecer.grave)
    criadas = _sample("revisao_pendente_created_total", reason="coerência das odds")

    materialization.materializar_aposta(7, grave, "h")
    materialization.materializar_aposta(7, grave, "h")

    assert parecer.grave
    assert parecer.falhas[0].forca not in GRAVES
    assert _sample("revisao_pendente_created_total", reason="coerência das odds") == (
        pytest.approx(criadas + 1)
    )


def test_review_reason_label_is_a_grave_conference_or_a_fixed_word() -> None:
    parecer = conferir(_multipla_acima_do_teto().para_bilhete(), origem=Origem.IA)
    duvida = conferir(_bom(confianca=0.3).para_bilhete(), origem=Origem.IA)
    bom = _bom().model_dump(mode="json")
    duvidoso = {"bilhete": _bom(confianca=0.3).model_dump(mode="json"), "motivo": duvida.motivo}

    def motivo_lido(extracao):
        leitura = materialization.ExtracaoDoJson.model_validate(extracao).para_leitura()
        (nova, *_) = materializar.apostas_da_leitura(
            leitura, chat_id=1, message_id=2, valor_unidade_centavos=10_000
        )
        return nova.revisao_motivo

    casos = {
        parecer.motivo: "coerência das odds",
        rodada.leitura_que_falhou(RuntimeError("boom")).motivo: "leitura falhou",
        motivo_lido(_extracao(cupons=[duvidoso, {"bilhete": bom}])): "cupons sem stake",
        motivo_lido(_extracao(bilhete=_bom(casa="Rodri").model_dump(mode="json"))): "outro",
        materialization.MOTIVO_GRAVE_PADRAO: "conferência grave",
        "apaguei porque era repetida": "outro",
    }

    assert {motivo: materialization.motivo_da_metrica(motivo) for motivo in casos} == casos
    assert not duvida.grave
    assert materialization.CONFERENCIAS_GRAVES == {
        "campos essenciais",
        "coerência das odds",
        "data do jogo",
        "evento plausível",
        "faixa das odds",
        "legibilidade",
        "odd turbinada",
    }


def test_materialization_counts_the_bets_it_wrote_and_times_the_stage(monkeypatch) -> None:
    _instalar(monkeypatch, _banco())
    cupom = {"bilhete": _bom().model_dump(mode="json"), "motivo": None, "grave": False}
    apostas = _sample("batch_bets_processed_total", stage="materialization")
    tempos = _sample("batch_job_duration_seconds_count", stage="materialization", status="success")

    materialization.materializar_aposta(7, _extracao(cupons=[cupom, cupom]), "h")

    assert _sample("batch_bets_processed_total", stage="materialization") == pytest.approx(
        apostas + 2
    )
    assert _sample(
        "batch_job_duration_seconds_count", stage="materialization", status="success"
    ) == pytest.approx(tempos + 1)


def test_invalid_payload_fails_the_stage_with_its_exception(monkeypatch) -> None:
    _instalar(monkeypatch, _banco())
    falhas = _sample("batch_failed_total", stage="materialization", reason="ValidationError")

    with pytest.raises(ValidationError):
        materialization.materializar_aposta(7, _extracao(chat_id=None), "h")

    assert _sample(
        "batch_failed_total", stage="materialization", reason="ValidationError"
    ) == pytest.approx(falhas + 1)


def test_extraction_counts_as_many_bets_as_materialization_writes() -> None:
    bom, ilegivel = _bom(), _bom(ilegivel=True)
    formatos = [
        (bilhete, motivo, nao_e_aposta, cupons)
        for bilhete in (None, bom, ilegivel)
        for motivo in (None, "a leitura falhou (APIError)")
        for nao_e_aposta in (False, True)
        for cupons in (
            (),
            (bom,),
            (ilegivel,),
            (bom, bom),
            (bom, ilegivel),
            (ilegivel, ilegivel),
            (bom, bom, ilegivel),
        )
    ]

    for bilhete, motivo, nao_e_aposta, cupons in formatos:
        leitura = rodada.LeituraDaMensagem(
            bilhete=bilhete,
            motivo=motivo,
            cupons=tuple(rodada.CupomLido(c) for c in cupons),
            nao_e_aposta=nao_e_aposta,
        )
        recebida = materialization.ExtracaoDoJson.model_validate({
            "chat_id": 1,
            "message_id": 2,
            **leitura.para_json(),
        }).para_leitura()
        novas = materializar.apostas_da_leitura(
            recebida, chat_id=1, message_id=2, valor_unidade_centavos=10_000
        )

        assert leitura.quantidade_de_apostas == len(novas), leitura
    assert len(formatos) == 84


def test_get_engine_uses_the_database_url_without_a_pool(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5433/bancaemdia")
    get_settings.cache_clear()
    materialization.get_engine.cache_clear()
    try:
        engine = materialization.get_engine()
    finally:
        get_settings.cache_clear()
        materialization.get_engine.cache_clear()

    assert (engine.url.host, engine.url.port) == ("db-host", 5433)
    assert isinstance(engine.pool, NullPool)


def _betano(id_="20753556039", *, resultado="Lose", ganho=0.0, odd=1.90, **extra):
    bruto = {
        "id": id_,
        "bonusType": 0,
        "totalAmount": 160.0,
        "totalAmountWithCurrency": {"amount": 160.0, "currencyCode": "BRL"},
        "totalOdds": odd,
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
                        "selections": [{"description": "Mais de 41.5", "odds": odd}],
                    }
                ]
            }
        ],
        **extra,
    }
    if resultado is None:
        del bruto["finalBetResult"], bruto["settledAt"]
    return bruto


def _guardada(monkeypatch, bruto, nome="Betano"):
    class Guardada:
        def __init__(self):
            self.bruto = bruto
            self.processadas = []

    guardada = Guardada()

    class ColetaCasaRepo:
        async def get_by_id_for_update(self, session, usuario_id, id_):
            if id_ != 11:
                return None
            return SimpleNamespace(
                id=11, usuario_id=usuario_id, casa_id=1, bruto_json=guardada.bruto
            )

        async def set_processado(self, session, usuario_id, id_):
            guardada.processadas.append(id_)

    class CasaRepo:
        async def get_nome_by_id(self, session, casa_id):
            return nome

    monkeypatch.setattr(materialization, "ColetaCasaRepo", ColetaCasaRepo)
    monkeypatch.setattr(materialization, "CasaRepo", CasaRepo)
    return guardada


def test_house_bet_becomes_a_bet_with_its_result_and_the_house_as_source(monkeypatch) -> None:
    banco = _banco(contas=[_conta(3)])
    _instalar(monkeypatch, banco)
    guardada = _guardada(monkeypatch, _betano())
    apostas = _sample("batch_bets_processed_total", stage="materialization")

    resultado = materialization.materializar_coleta(7, 11)

    chave = "c:betano:20753556039"
    assert resultado == {
        "usuario_id": 7,
        "coleta_id": 11,
        "aposta": chave,
        "criada": True,
        "eventos": 2,
        "revisao": False,
    }
    assert [(e["tipo"], e["fonte"], e["aposta_chave"]) for e in banco.eventos] == [
        ("APOSTA_CRIADA", "casa", chave),
        ("RESULTADO_REGISTRADO", "casa", chave),
    ]
    assert banco.eventos[0]["chat_id"] is None
    (aposta,) = banco.upserts
    jogo = datetime(2026, 8, 2, 22, 30, tzinfo=UTC)
    assert (aposta["origem"], aposta["estado"], aposta["retorno_centavos"]) == ("casa", "RED", 0)
    assert (aposta["stake_centavos"], aposta["data_aposta"], aposta["conta_casa_id"]) == (
        16000,
        jogo,
        3,
    )
    assert banco.contas_buscadas == [("Betano", jogo)]
    assert guardada.processadas == [11]
    assert banco.publicados == [ApostaCriada(7, chave, "casa", False)]
    assert _sample("batch_bets_processed_total", stage="materialization") == pytest.approx(
        apostas + 1
    )


def test_open_house_bet_that_settles_gets_the_result_the_house_paid(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    guardada = _guardada(monkeypatch, _betano(resultado=None))
    materialization.materializar_coleta(7, 11)
    guardada.bruto = _betano(resultado="Win", ganho=304.0)

    segunda = materialization.materializar_coleta(7, 11)

    assert (segunda["criada"], segunda["eventos"]) == (False, 1)
    assert [e["tipo"] for e in banco.eventos] == ["APOSTA_CRIADA", "RESULTADO_REGISTRADO"]
    assert banco.eventos[1]["payload_json"]["retorno_centavos"] == 30400
    assert (banco.upserts[-1]["estado"], banco.upserts[-1]["retorno_centavos"]) == ("GREEN", 30400)
    assert "conta_casa_id" not in banco.upserts[-1]
    assert len(banco.publicados) == 1


def test_held_house_bet_opens_one_review_for_its_row(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    _guardada(monkeypatch, _betano(bonusType=3))
    criadas = _sample("revisao_pendente_created_total", reason="outro")

    materialization.materializar_coleta(7, 11)
    materialization.materializar_coleta(7, 11)

    (revisao,) = banco.revisoes
    assert "bonusType=3" in revisao["motivo"]
    assert revisao["extracao_bruta"] == {"aposta_chave": "c:betano:20753556039", "coleta_id": 11}
    assert revisao["midia_hash"] is None
    assert _sample("revisao_pendente_created_total", reason="outro") == pytest.approx(criadas + 1)


def test_house_bet_the_product_cannot_count_fails_without_retry(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    guardada = _guardada(monkeypatch, _betano(odd=1.0))

    with pytest.raises(ApostaInvalidaError, match=r"1\.01"):
        materialization.materializar_coleta(7, 11)

    assert ApostaInvalidaError not in materialization.RETRY_ON
    assert (banco.eventos, guardada.processadas) == ([], [])


def test_missing_row_or_house_without_a_reader_writes_nothing(monkeypatch) -> None:
    banco = _banco()
    _instalar(monkeypatch, banco)
    _guardada(monkeypatch, _betano())

    sem_linha = materialization.materializar_coleta(7, 99)
    _guardada(monkeypatch, {"ticket": 1}, nome="bet365")
    sem_leitor = materialization.materializar_coleta(7, 11)

    assert sem_linha["aposta"] is None
    assert sem_leitor["aposta"] is None
    assert banco.eventos == []


def test_coleta_task_is_routed_and_retried_like_readings() -> None:
    task = materialization.materializar_coleta_task

    assert task.name == "materialization.materializar_coleta"
    assert celery_app.app.amqp.router.route({}, task.name)["queue"].name == "materialization"
    assert set(task.autoretry_for) == set(materialization.RETRY_ON)
    assert task.max_retries == 3
