from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from celery import chain

from bancaemdia.config import get_settings
from bancaemdia.domain import upload as dominio
from bancaemdia.domain.registros import Upload, UploadBilhete
from bancaemdia.workers import extraction, materialization
from bancaemdia.workers import upload as trabalho

PASTA = "ChatExport_2026-07-24/"
CHAT = 555
USUARIO = 7


def _mensagem(message_id, **extra):
    return {
        "id": message_id,
        "type": "message",
        "date": "2026-07-24T16:17:52",
        "from": "girafalles",
        "text": "1u",
        **extra,
    }


def _zip_do_export(mensagens, fotos):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as arquivo:
        arquivo.writestr(
            PASTA + "result.json",
            json.dumps({"name": "Canal", "type": "channel", "id": CHAT, "messages": mensagens}),
        )
        for nome, conteudo in fotos:
            arquivo.writestr(PASTA + nome, conteudo)
    return buffer.getvalue()


def _upload(status="pending", job_id=None):
    return Upload(
        id=1,
        job_id=job_id or uuid4(),
        usuario_id=USUARIO,
        filename="ChatExport.zip",
        chat_id=CHAT,
        status=status,
        total_messages=0,
        estimated_bets=0,
        estimated_cost_usd=Decimal("0.0108"),
        bets_processed=0,
        bets_failed=0,
        cost_usd=Decimal(0),
        erro=None,
        criado_em=datetime.now(UTC),
        concluido_em=None,
    )


def _bilhete(id_, message_id, midia_hash="h1", estado="PENDENTE"):
    return UploadBilhete(
        id=id_,
        upload_id=1,
        usuario_id=USUARIO,
        chat_id=CHAT,
        message_id=message_id,
        midia_hash=midia_hash,
        estado=estado,
        apostas=0,
        custo_usd=Decimal(0),
        enfileirado_em=None,
        criado_em=datetime.now(UTC),
    )


def _banco(monkeypatch, *, arquivo=None, upload=None, pendentes=(), por_estado=None):
    class Banco:
        def __init__(self):
            self.mensagens = []
            self.midias = []
            self.arquivos_de_midia = {}
            self.bilhetes = []
            self.status = []
            self.contagem = []
            self.apagados = []
            self.enfileirados = []
            self.fechados = []
            self.tarefas = []
            self.avisos = []

    banco = Banco()

    class Sessao:
        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, *_args, **_kwargs):
            return None

    class Conexao:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Motor:
        def connect(self):
            return Conexao()

    class MensagemRepo:
        async def save(self, session, **dados):
            banco.mensagens.append(dados)
            return len(banco.mensagens), "nova"

        async def get_by_chat_message(self, session, chat_id, message_id):
            # O banco devolve timestamptz em UTC: a mensagem das 21h no Brasil volta como 00h.
            return SimpleNamespace(
                texto="1u https://betano.bet.br/x @1.91",
                data=datetime(2026, 9, 11, 0, 0, tzinfo=UTC),
            )

    class MidiaRepo:
        async def upsert_idempotent(self, session, hash_, tipo, bytes_, mensagem_id=None):
            banco.midias.append((hash_, tipo, bytes_, mensagem_id))

    class MidiaArquivoRepo:
        async def upsert_idempotent(self, session, hash_, conteudo):
            banco.arquivos_de_midia[hash_] = conteudo

        async def get_by_hash(self, session, hash_):
            return banco.arquivos_de_midia.get(hash_, b"JPEG")

    class UploadRepo:
        async def get_by_id_for_update(self, session, usuario_id, upload_id):
            return upload

        async def set_status(self, session, usuario_id, upload_id, status, erro=None):
            banco.status.append((status, erro))

        async def set_contagem_do_export(self, session, usuario_id, upload_id, mensagens, chat_id):
            banco.contagem.append((mensagens, chat_id))

    class UploadBilheteRepo:
        async def insert_many_idempotent(self, session, linhas):
            banco.bilhetes.extend(linhas)

        async def count_enviados_desde(self, session, usuario_id, desde):
            banco.desde = desde
            return 0

        async def list_pendentes(self, session, usuario_id, upload_id):
            return list(pendentes)

        async def mark_enfileirado(self, session, usuario_id, bilhete_id):
            banco.enfileirados.append(bilhete_id)

        async def finish(self, session, usuario_id, upload_id, chat, message, estado, *resto):
            banco.fechados.append((message, estado, *resto))
            return len(banco.fechados)

        async def count_by_estado(self, session, usuario_id, upload_id):
            return dict(por_estado or {})

        async def somar_resultado(self, session, usuario_id, upload_id):
            return 4, 1, Decimal("0.0216")

    class UploadArquivoRepo:
        async def get_by_upload_id(self, session, usuario_id, upload_id):
            return arquivo

        async def delete_by_upload_id(self, session, usuario_id, upload_id):
            banco.apagados.append(upload_id)

    for modulo in (trabalho, materialization):
        monkeypatch.setattr(modulo, "get_engine", lambda: Motor(), raising=False)
        monkeypatch.setattr(modulo, "AsyncSession", lambda **_: Sessao(), raising=False)
        for nome, classe in (
            ("UploadRepo", UploadRepo),
            ("UploadBilheteRepo", UploadBilheteRepo),
            ("UploadArquivoRepo", UploadArquivoRepo),
            ("MensagemRepo", MensagemRepo),
            ("MidiaRepo", MidiaRepo),
            ("MidiaArquivoRepo", MidiaArquivoRepo),
        ):
            if hasattr(modulo, nome):
                monkeypatch.setattr(modulo, nome, classe)
        monkeypatch.setattr(
            modulo.app,
            "send_task",
            lambda nome, **opcoes: banco.tarefas.append((nome, opcoes)),
        )
    # O aviso de fim sai por HTTP: quem prova o webhook chama a função original guardada aqui.
    banco.notificar = trabalho.notificar_upload
    monkeypatch.setattr(trabalho, "notificar_upload", lambda *args: banco.avisos.append(args))
    return banco


def test_the_chain_hands_the_reading_to_the_write_in_the_first_slot() -> None:
    cadeia = extraction.cadeia_do_bilhete(
        USUARIO,
        "Zm90bw==",
        nome_do_arquivo="h1.jpg",
        legenda="1u",
        postada_em="2026-07-24T16:17:52",
        casas_do_link=["Betano"],
        odds_do_texto=[1.91],
        chat_id=CHAT,
        message_id=3,
        midia_hash="h1",
        upload_id=1,
    )

    leitura, gravacao = cadeia.tasks
    assert leitura.task == "extraction.extrair_bilhete"
    assert leitura.kwargs["imagem_base64"] == "Zm90bw=="
    assert leitura.kwargs["casas_do_link"] == ["Betano"]
    assert leitura.kwargs["odds_do_texto"] == [1.91]
    assert gravacao.task == "materialization.materializar_leitura"
    # A leitura entra como primeiro argumento posicional: `materializar_aposta` tem o usuário
    # nessa posição e a corrente trocaria os dois sem erro nenhum.
    assert set(gravacao.kwargs) == {"usuario_id", "midia_hash", "upload_id"}
    assert gravacao.args == ()
    [errback] = cadeia.options["link_error"]
    assert errback.task == "materialization.registrar_falha"
    assert errback.args == (1, USUARIO, CHAT, 3)
    assert errback.immutable


def test_a_chain_outside_an_upload_has_no_error_callback() -> None:
    cadeia = extraction.cadeia_do_bilhete(
        USUARIO,
        "Zm90bw==",
        nome_do_arquivo="h1.jpg",
        legenda="",
        postada_em=None,
        casas_do_link=[],
        odds_do_texto=[],
        chat_id=CHAT,
        message_id=3,
        midia_hash="h1",
    )

    assert "link_error" not in cadeia.options
    assert cadeia.tasks[1].kwargs["upload_id"] is None


def test_celery_hands_the_previous_result_to_the_first_positional_parameter(monkeypatch) -> None:
    # A regra que a corrente usa, com tarefas de teste: o retorno da anterior entra na PRIMEIRA
    # posição. Por isso a gravação do upload recebe a leitura ali, e não o usuário.
    recebidos = {}

    def leitura_falsa():
        return {"chat_id": CHAT, "message_id": 3}

    def gravacao_boa(extracao_json, usuario_id, midia_hash=None, upload_id=None):
        recebidos.update(extracao=extracao_json, usuario=usuario_id, midia=midia_hash)
        return "ok"

    def gravacao_como_materializar_aposta(usuario_id, extracao_json, midia_hash=None):
        return "nunca chega aqui"

    app = extraction.app
    leitura = app.task(name="teste.leitura")(leitura_falsa)
    boa = app.task(name="teste.gravacao_boa")(gravacao_boa)
    trocada = app.task(name="teste.gravacao_trocada")(gravacao_como_materializar_aposta)
    monkeypatch.setitem(app.tasks, "teste.leitura", leitura)
    monkeypatch.setitem(app.tasks, "teste.gravacao_boa", boa)
    monkeypatch.setitem(app.tasks, "teste.gravacao_trocada", trocada)

    chain(leitura.si(), boa.s(usuario_id=USUARIO, midia_hash="h1", upload_id=1)).apply().get()

    assert recebidos == {
        "extracao": {"chat_id": CHAT, "message_id": 3},
        "usuario": USUARIO,
        "midia": "h1",
    }
    with pytest.raises(TypeError, match="usuario_id"):
        chain(leitura.si(), trocada.s(usuario_id=USUARIO)).apply().get()


def test_the_export_becomes_messages_photos_and_bills(monkeypatch) -> None:
    mensagens = [
        _mensagem(1, photo="photos/1.jpg"),
        _mensagem(2, photo="photos/2.jpg", text="ROI 2,49% no mês"),
        _mensagem(3),
    ]
    fotos = [("photos/1.jpg", b"JPEG-1"), ("photos/2.jpg", b"JPEG-2")]
    banco = _banco(
        monkeypatch, arquivo=_zip_do_export(mensagens, fotos), upload=_upload(), pendentes=()
    )

    trabalho.processar_upload(1, USUARIO)

    assert banco.status == [("processing", None)]
    assert [m["message_id"] for m in banco.mensagens] == [1, 2, 3]
    assert len(banco.midias) == 2
    assert set(banco.arquivos_de_midia.values()) == {b"JPEG-1", b"JPEG-2"}
    assert [(b["message_id"], b["estado"]) for b in banco.bilhetes] == [
        (1, "PENDENTE"),
        (2, "IGNORADO"),
    ]
    assert banco.contagem == [(3, CHAT)]
    assert banco.apagados == [1]


def test_a_photo_that_cannot_be_read_fails_its_bill_instead_of_hanging(monkeypatch) -> None:
    mensagens = [_mensagem(1, photo="photos/1.jpg"), _mensagem(2, photo="photos/2.jpg")]
    fotos = [("photos/1.jpg", b"JPEG-1"), ("photos/2.jpg", b"J" * 40)]
    banco = _banco(monkeypatch, arquivo=_zip_do_export(mensagens, fotos), upload=_upload())
    monkeypatch.setattr(dominio, "MAXIMO_POR_FOTO", 10)

    trabalho.processar_upload(1, USUARIO)

    # Sem isto o bilhete ficaria PENDENTE para sempre e o envio nunca fecharia. IGNORADO, e nao
    # FALHOU: a IA nunca viu esta foto, entao ela nao gasta vaga do teto do dia.
    assert [(b["message_id"], b["estado"]) for b in banco.bilhetes] == [
        (1, "PENDENTE"),
        (2, "IGNORADO"),
    ]


def test_photos_over_the_daily_ceiling_are_kept_for_another_day(monkeypatch) -> None:
    mensagens = [_mensagem(i, photo=f"photos/{i}.jpg") for i in range(1, 4)]
    fotos = [(f"photos/{i}.jpg", f"JPEG-{i}".encode()) for i in range(1, 4)]
    banco = _banco(monkeypatch, arquivo=_zip_do_export(mensagens, fotos), upload=_upload())
    monkeypatch.setenv("UPLOAD_DAILY_PHOTO_LIMIT", "2")
    get_settings.cache_clear()
    try:
        trabalho.processar_upload(1, USUARIO)
    finally:
        get_settings.cache_clear()

    assert [b["estado"] for b in banco.bilhetes] == ["PENDENTE", "PENDENTE", "TETO"]


def test_a_redelivered_upload_only_queues_what_is_left(monkeypatch) -> None:
    banco = _banco(
        monkeypatch,
        arquivo=None,
        upload=_upload("processing"),
        pendentes=[
            _bilhete(10, 1),
            _bilhete(11, 2),
        ],
    )
    enviados = []
    monkeypatch.setattr(
        trabalho,
        "cadeia_do_bilhete",
        lambda *args, **kwargs: SimpleNamespace(apply_async=lambda: enviados.append(kwargs)),
    )

    resultado = trabalho.processar_upload(1, USUARIO)

    assert banco.mensagens == []
    assert [e["message_id"] for e in enviados] == [1, 2]
    assert banco.enfileirados == [10, 11]
    assert resultado["bilhetes"] == 2


def test_the_queued_bill_carries_the_photo_caption_houses_and_odds(monkeypatch) -> None:
    _banco(monkeypatch, upload=_upload("processing"), pendentes=[_bilhete(10, 1)])
    enviados = []
    monkeypatch.setattr(
        trabalho,
        "cadeia_do_bilhete",
        lambda *args, **kwargs: SimpleNamespace(
            apply_async=lambda: enviados.append((args, kwargs))
        ),
    )

    trabalho.processar_upload(1, USUARIO)

    (usuario, imagem), kwargs = enviados[0]
    assert usuario == USUARIO
    assert base64.b64decode(imagem) == b"JPEG"
    assert kwargs["legenda"] == "1u https://betano.bet.br/x @1.91"
    assert kwargs["casas_do_link"] == ["Betano"]
    assert kwargs["odds_do_texto"] == [1.91]
    # A leitura recebe a hora de quem postou, sem fuso, como a gravacao da aposta supoe: mandar a
    # hora em UTC jogaria a aposta 3 horas para a frente, e as vezes para o dia seguinte.
    assert kwargs["postada_em"] == "2026-09-10T21:00:00"
    assert kwargs["upload_id"] == 1


def test_an_upload_with_nothing_to_read_is_reported_at_once(monkeypatch) -> None:
    banco = _banco(monkeypatch, arquivo=_zip_do_export([_mensagem(1)], []), upload=_upload())
    trabalho.processar_upload(1, USUARIO)

    assert banco.tarefas == [
        ("materialization.notificar_upload", {"kwargs": {"upload_id": 1, "usuario_id": USUARIO}})
    ]


def test_a_stored_export_that_does_not_open_fails_the_upload(monkeypatch) -> None:
    banco = _banco(monkeypatch, arquivo=b"nao e um zip", upload=_upload())
    resultado = trabalho.processar_upload(1, USUARIO)

    assert resultado["status"] == "failed"
    assert banco.status[-1] == ("failed", "o export guardado não abriu")
    assert banco.apagados == [1]
    assert banco.tarefas[0][0] == "materialization.notificar_upload"


def test_a_read_bill_is_closed_and_the_upload_is_reported_when_nothing_is_pending(
    monkeypatch,
) -> None:
    banco = _banco(monkeypatch, upload=_upload("processing"), por_estado={"LIDO": 2})
    monkeypatch.setattr(
        materialization,
        "materializar_aposta",
        lambda usuario_id, extracao_json, midia_hash=None: {"apostas": ["t:555:3:0"]},
    )

    materialization.materializar_leitura(
        {"chat_id": CHAT, "message_id": 3, "custo_usd": 0.004}, USUARIO, "h1", 1
    )

    assert banco.fechados == [(3, "LIDO", 1, Decimal("0.004000"))]
    assert banco.tarefas == [
        ("materialization.notificar_upload", {"kwargs": {"upload_id": 1, "usuario_id": USUARIO}})
    ]


def test_a_bill_still_pending_does_not_report_the_upload(monkeypatch) -> None:
    banco = _banco(monkeypatch, upload=_upload("processing"), por_estado={"PENDENTE": 1, "LIDO": 1})
    monkeypatch.setattr(
        materialization,
        "materializar_aposta",
        lambda usuario_id, extracao_json, midia_hash=None: {"apostas": []},
    )

    materialization.materializar_leitura(
        {"chat_id": CHAT, "message_id": 3, "custo_usd": 0.0}, USUARIO, "h1", 1
    )

    assert banco.tarefas == []


def test_a_reading_outside_an_upload_touches_no_upload_row(monkeypatch) -> None:
    banco = _banco(monkeypatch, upload=_upload("processing"), por_estado={"LIDO": 1})
    monkeypatch.setattr(
        materialization,
        "materializar_aposta",
        lambda usuario_id, extracao_json, midia_hash=None: {"apostas": ["t:555:3:0"]},
    )

    materialization.materializar_leitura({"chat_id": CHAT, "message_id": 3}, USUARIO, "h1")

    assert (banco.fechados, banco.tarefas) == ([], [])


def test_a_failed_reading_closes_its_bill_as_failed(monkeypatch) -> None:
    banco = _banco(monkeypatch, upload=_upload("processing"), por_estado={"FALHOU": 1})

    materialization.registrar_falha(1, USUARIO, CHAT, 3)

    assert banco.fechados == [(3, "FALHOU", 0, Decimal("0.000000"))]
    assert banco.tarefas[0][0] == "materialization.notificar_upload"


def test_the_worker_calls_the_webhook_with_the_shared_secret(monkeypatch) -> None:
    banco = _banco(monkeypatch, upload=_upload("processing"))
    pedidos = []

    def responder(request):
        pedidos.append((str(request.url), json.loads(request.content), dict(request.headers)))
        return httpx.Response(204)

    monkeypatch.setattr(
        trabalho,
        "get_webhook_client",
        lambda: httpx.Client(
            base_url=get_settings().API_INTERNAL_URL or "",
            transport=httpx.MockTransport(responder),
        ),
    )

    corpo = banco.notificar(1, USUARIO)

    url, enviado, headers = pedidos[0]
    assert url.endswith(trabalho.WEBHOOK_PATH)
    assert enviado["status"] == "completed"
    assert (enviado["bets_processed"], enviado["bets_failed"]) == (4, 1)
    assert enviado["cost_usd"] == pytest.approx(0.0216)
    assert headers[trabalho.WEBHOOK_HEADER.lower()] == get_settings().UPLOAD_WEBHOOK_SECRET
    assert corpo["job_id"]


def test_a_failed_upload_is_reported_as_failed(monkeypatch) -> None:
    banco = _banco(monkeypatch, upload=_upload("failed"))
    enviados = []

    def responder(request):
        enviados.append(json.loads(request.content))
        return httpx.Response(204)

    monkeypatch.setattr(
        trabalho,
        "get_webhook_client",
        lambda: httpx.Client(
            base_url=get_settings().API_INTERNAL_URL or "",
            transport=httpx.MockTransport(responder),
        ),
    )

    banco.notificar(1, USUARIO)

    assert enviados[0]["status"] == "failed"


def test_a_worker_without_the_webhook_settings_says_so_instead_of_reporting(monkeypatch) -> None:
    banco = _banco(monkeypatch, upload=_upload("processing"))
    monkeypatch.setenv("API_INTERNAL_URL", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(trabalho.WebhookSemDestinoError):
            banco.notificar(1, USUARIO)
    finally:
        get_settings.cache_clear()


def test_every_upload_task_is_registered_on_the_materialization_queue() -> None:
    nomes = (
        "materialization.processar_upload",
        "materialization.falhar_upload",
        "materialization.notificar_upload",
        "materialization.materializar_leitura",
        "materialization.registrar_falha",
    )

    for nome in nomes:
        assert nome in trabalho.app.tasks
        assert trabalho.app.amqp.router.route({}, nome)["queue"].name == "materialization"
