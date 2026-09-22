from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi.testclient import TestClient
from jose import jwk, jwt
from kombu.exceptions import OperationalError
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from bancaemdia import main
from bancaemdia.api import deps
from bancaemdia.api.v1 import upload as rota
from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.config import get_settings
from bancaemdia.core.context import use_primary
from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Upload, Usuario
from bancaemdia.extracao.precos import USD_POR_BILHETE_REFERENCIA
from bancaemdia.middleware.rate_limit import upload_limiter
from bancaemdia.middleware.router import RouterMiddleware

USUARIO = 7
PASTA = "ChatExport_2026-07-24/"


@pytest.fixture(autouse=True)
def _disable_cross_cutting_upload_limit(monkeypatch):
    # This file exercises upload validation and persistence. The middleware contract, including
    # the 1/5-minute bucket, has isolated tests of its own.
    monkeypatch.setattr(upload_limiter, "enabled", False)


@pytest.fixture(scope="module")
def chave():
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = par.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    publica = par.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return privada, {**jwk.construct(publica, "RS256").to_dict(), "kid": "k1"}


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


def _mensagem(message_id, **extra):
    return {
        "id": message_id,
        "type": "message",
        "date": "2026-07-24T16:17:52",
        "from": "girafalles",
        "text": "1u",
        **extra,
    }


def _zip_do_export(quantas_fotos=2, mensagens_sem_foto=1, chat_id=555):
    mensagens = [
        _mensagem(i, photo=f"photos/photo_{i}.jpg") for i in range(1, quantas_fotos + 1)
    ] + [_mensagem(quantas_fotos + i) for i in range(1, mensagens_sem_foto + 1)]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as arquivo:
        arquivo.writestr(
            PASTA + "result.json",
            json.dumps({
                "name": "Canal",
                "type": "private_channel",
                "id": chat_id,
                "messages": mensagens,
            }),
        )
        for i in range(1, quantas_fotos + 1):
            arquivo.writestr(PASTA + f"photos/photo_{i}.jpg", f"JPEG-{i}".encode())
    return buffer.getvalue()


def _banco(ultimo_envio=None, ja_hoje=0, upload=None, trava_livre=True):
    class Banco:
        def __init__(self):
            self.uploads = []
            self.arquivos = {}
            self.enfileirados = []
            self.commits = 0
            self.status = []
            self.sql = []
            self.fechados = []
            self.escolhas = []
            self.rollbacks = 0

    banco = Banco()

    class UploadRepo:
        async def get_ultimo_aceito_em(self, session, usuario_id):
            return ultimo_envio

        async def create(self, session, dados):
            linha = Upload(
                id=len(banco.uploads) + 1,
                job_id=uuid4(),
                usuario_id=dados["usuario_id"],
                filename=dados["filename"],
                chat_id=dados["chat_id"],
                status="pending",
                total_messages=dados["total_messages"],
                estimated_bets=dados["estimated_bets"],
                estimated_cost_usd=dados["estimated_cost_usd"],
                bets_processed=0,
                bets_failed=0,
                cost_usd=Decimal(0),
                erro=None,
                criado_em=datetime.now(UTC),
                concluido_em=None,
            )
            banco.uploads.append(linha)
            return linha

        async def get_by_job_id(self, session, usuario_id, job_id):
            return upload if upload is not None and upload.usuario_id == usuario_id else None

        async def set_status(self, session, usuario_id, upload_id, status, erro=None):
            banco.status.append((upload_id, status, erro))

        async def finish_by_job_id(self, session, job_id, status, processadas, falhas, custo):
            if upload is None or upload.job_id != job_id or upload.status == "pending":
                return None
            banco.fechados.append((job_id, status, processadas, falhas, custo))
            return upload.id

    class UploadBilheteRepo:
        async def count_enviados_desde(self, session, usuario_id, desde):
            banco.desde = desde
            return ja_hoje

        async def count_by_estado(self, session, usuario_id, upload_id):
            return {"LIDO": 3, "PENDENTE": 1, "IGNORADO": 2}

    class UploadArquivoRepo:
        async def create(self, session, usuario_id, upload_id, conteudo):
            banco.arquivos[upload_id] = conteudo

        async def delete_by_upload_id(self, session, usuario_id, upload_id):
            banco.arquivos.pop(upload_id, None)

    class Session:
        async def execute(self, statement, params=None):
            banco.sql.append((str(statement), params))

        async def scalar(self, statement, params=None):
            banco.sql.append((str(statement), params))
            return trava_livre

        async def commit(self):
            banco.commits += 1

        async def rollback(self):
            banco.rollbacks += 1

    class Sessoes:
        async def abrir(self):
            banco.escolhas.append(use_primary.get())
            yield Session()

    banco.repos = {
        "UploadRepo": UploadRepo,
        "UploadBilheteRepo": UploadBilheteRepo,
        "UploadArquivoRepo": UploadArquivoRepo,
    }
    banco.sessao = Sessoes().abrir
    return banco


def _usuario(id_=USUARIO, ativo=True):
    return Usuario(
        id=id_, email=f"{id_}@teste.local", nome=f"U{id_}", criado_em=datetime.now(UTC), ativo=ativo
    )


def _cliente(monkeypatch, chave, banco, usuario=None, falha_da_fila=None):
    _, publica = chave

    class UsuarioRepo:
        async def get_by_id(self, session, id_):
            pessoa = _usuario() if usuario is None else usuario
            return pessoa if pessoa.id == id_ else None

    class Fila:
        def enfileirar(self, upload_id, usuario_id):
            if falha_da_fila is not None:
                raise falha_da_fila
            banco.enfileirados.append((upload_id, usuario_id))

    for nome, classe in banco.repos.items():
        monkeypatch.setattr(rota, nome, classe)
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: _chaves(publica))
    monkeypatch.setattr(deps, "UsuarioRepo", UsuarioRepo)
    monkeypatch.setattr(rota, "_enfileirar", Fila().enfileirar)
    monkeypatch.setitem(main.app.dependency_overrides, get_db, banco.sessao)
    return TestClient(main.app)


def _enviar(cliente, privada, conteudo=None, nome="ChatExport.zip", **campos):
    arquivos = {"file": (nome, io.BytesIO(_zip_do_export() if conteudo is None else conteudo))}
    return cliente.post(
        "/api/v1/upload",
        files=arquivos,
        data=campos,
        headers={"Authorization": f"Bearer {_token(privada)}"},
    )


def test_a_telegram_export_is_accepted_with_its_estimate(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada)

    assert resposta.status_code == 202
    corpo = resposta.json()
    assert corpo["estimated_bets"] == 2
    assert corpo["estimated_cost_usd"] == pytest.approx(2 * USD_POR_BILHETE_REFERENCIA)
    assert corpo["status_url"] == f"/api/v1/upload/{corpo['job_id']}"
    assert UUID(corpo["job_id"])
    assert "aviso" not in corpo
    [linha] = banco.uploads
    assert (linha.filename, linha.chat_id, linha.total_messages) == ("ChatExport.zip", 555, 3)
    assert banco.arquivos[linha.id]
    assert banco.enfileirados == [(linha.id, USUARIO)]
    # A transação das conferências fecha antes de o corpo ser lido: o arquivo sobe sem prender
    # uma conexão do banco.
    assert banco.rollbacks == 1 and banco.commits == 1


def test_an_export_without_photos_says_so_instead_of_reporting_nothing(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()

    resposta = _enviar(
        _cliente(monkeypatch, chave, banco), privada, conteudo=_zip_do_export(quantas_fotos=0)
    )

    assert resposta.status_code == 202
    assert resposta.json()["estimated_bets"] == 0
    assert "nenhuma foto" in resposta.json()["aviso"]


def test_an_upload_without_a_token_is_refused(monkeypatch, chave) -> None:
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = cliente.post("/api/v1/upload", files={"file": ("x.zip", io.BytesIO(b"zip"))})

    assert resposta.status_code == 401
    assert banco.uploads == []


def test_a_second_upload_within_five_minutes_waits(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco(ultimo_envio=datetime.now(UTC) - timedelta(minutes=1))

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada)

    assert resposta.status_code == 429
    assert "a cada 5 minutos" in resposta.json()["detail"]
    assert 0 < int(resposta.headers["Retry-After"]) <= 241
    assert banco.uploads == []


def test_an_upload_older_than_the_interval_goes_through(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco(ultimo_envio=datetime.now(UTC) - timedelta(minutes=6))

    assert _enviar(_cliente(monkeypatch, chave, banco), privada).status_code == 202


def test_the_daily_photo_ceiling_refuses_and_explains(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco(ja_hoje=get_settings().UPLOAD_DAILY_PHOTO_LIMIT)

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada)

    assert resposta.status_code == 429
    assert "teto de 200 fotos por dia" in resposta.json()["detail"]
    assert banco.uploads == []


def test_photos_over_the_ceiling_are_left_for_the_next_day(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco(ja_hoje=get_settings().UPLOAD_DAILY_PHOTO_LIMIT - 1)

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada)

    assert resposta.status_code == 202
    assert resposta.json()["estimated_bets"] == 1
    assert "1 fotos ficaram para depois" in resposta.json()["aviso"]


def test_the_ceiling_counts_the_day_in_brazil(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()

    _enviar(_cliente(monkeypatch, chave, banco), privada)

    assert banco.desde.utcoffset() == timedelta(hours=-3)
    assert (banco.desde.hour, banco.desde.minute) == (0, 0)


def test_an_export_that_is_not_a_telegram_one_is_refused_with_the_reason(
    monkeypatch, chave
) -> None:
    privada, _ = chave
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada, conteudo=b"nao e um zip")

    assert resposta.status_code == 422
    assert "não consegui abrir este zip" in resposta.json()["detail"]
    assert banco.uploads == []


def test_a_refused_export_does_not_burn_the_five_minute_interval(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco)

    recusado = _enviar(cliente, privada, conteudo=b"nao e um zip")
    aceito = _enviar(cliente, privada)

    assert (recusado.status_code, aceito.status_code) == (422, 202)


def test_another_extension_is_refused(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada, nome="export.rar")

    assert resposta.status_code == 422
    assert "Telegram Desktop" in resposta.json()["detail"]


def test_an_upload_without_the_file_field_is_refused(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = cliente.post(
        "/api/v1/upload",
        data={"chat_filter": "555"},
        headers={"Authorization": f"Bearer {_token(privada)}"},
    )

    assert resposta.status_code == 422
    assert "campo `file`" in resposta.json()["detail"]


def test_a_chat_filter_keeps_only_the_chats_it_names(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco)

    de_outro_chat = _enviar(cliente, privada, chat_filter="999,1000")
    banco.uploads.clear()
    do_chat = _enviar(cliente, privada, chat_filter="999, 555")

    assert de_outro_chat.status_code == 422
    assert "não está no `chat_filter`" in de_outro_chat.json()["detail"]
    assert do_chat.status_code == 202


def test_a_chat_filter_that_is_not_a_number_is_refused(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada, chat_filter="canal")

    assert resposta.status_code == 422
    assert "só aceita números" in resposta.json()["detail"]


def test_a_body_over_the_limit_is_refused_by_the_declared_size(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = cliente.post(
        "/api/v1/upload",
        content=b"x" * 100,
        headers={
            "Authorization": f"Bearer {_token(privada)}",
            "Content-Type": "application/zip",
            "Content-Length": str(get_settings().UPLOAD_MAX_BYTES + 1024),
        },
    )

    assert resposta.status_code == 413
    assert "passa de 50 MB" in resposta.json()["detail"]
    assert banco.uploads == []


def test_a_body_over_the_limit_is_cut_while_streaming(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco)
    limite = get_settings().UPLOAD_MAX_BYTES
    fronteira = "fronteira-do-teste"

    fim_de_linha = chr(13) + chr(10)
    cabecalho = (
        f"--{fronteira}{fim_de_linha}"
        f'Content-Disposition: form-data; name="file"; filename="ChatExport.zip"{fim_de_linha}'
        f"Content-Type: application/zip{fim_de_linha}{fim_de_linha}"
    )

    def pedacos():
        # Sem Content-Length, o corpo só é cortado enquanto chega: é o caso que a conferência
        # do cabeçalho não pega.
        yield cabecalho.encode()
        for _ in range(limite // (1024 * 1024) + 2):
            yield b"x" * (1024 * 1024)
        yield f"{fim_de_linha}--{fronteira}--{fim_de_linha}".encode()

    resposta = cliente.post(
        "/api/v1/upload",
        content=pedacos(),
        headers={
            "Authorization": f"Bearer {_token(privada)}",
            "Content-Type": f"multipart/form-data; boundary={fronteira}",
        },
    )

    assert resposta.status_code == 413
    assert banco.uploads == []


def test_the_body_limit_runs_inside_the_other_middlewares() -> None:
    # Registrado por fora de um BaseHTTPMiddleware, o 413 dele vira 500 (medido).
    classes = [camada.cls for camada in main.app.user_middleware]

    assert classes.index(RequestBodyLimitMiddleware) > classes.index(RouterMiddleware)


def test_an_export_of_the_same_person_already_being_read_waits(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco(trava_livre=False)

    resposta = _enviar(_cliente(monkeypatch, chave, banco), privada)

    # A trava não espera: o trabalhador a segura enquanto lê um export, e esperar por ela
    # estouraria o limite de 5 s do banco em cima deste pedido.
    assert resposta.status_code == 429
    assert "espere um pouco" in resposta.json()["detail"]
    assert banco.uploads == []
    assert any("pg_try_advisory_xact_lock" in sql for sql, _ in banco.sql)


def test_a_queue_that_is_down_marks_the_upload_as_failed(monkeypatch, chave) -> None:
    privada, _ = chave
    banco = _banco()
    cliente = _cliente(monkeypatch, chave, banco, falha_da_fila=OperationalError("fila fora"))

    resposta = _enviar(cliente, privada)

    assert resposta.status_code == 503
    assert "mande de novo em instantes" in resposta.json()["detail"]
    assert banco.status == [(1, "failed", "a fila estava fora do ar")]
    assert banco.arquivos == {}


def _upload_guardado(status="processing", job_id=None, usuario_id=USUARIO):
    return Upload(
        id=1,
        job_id=job_id or uuid4(),
        usuario_id=usuario_id,
        filename="ChatExport.zip",
        chat_id=555,
        status=status,
        total_messages=10,
        estimated_bets=6,
        estimated_cost_usd=Decimal("0.032400"),
        bets_processed=3,
        bets_failed=1,
        cost_usd=Decimal("0.021600"),
        erro=None,
        criado_em=datetime.now(UTC),
        concluido_em=None,
    )


def test_the_status_of_an_upload_shows_its_progress(monkeypatch, chave) -> None:
    privada, _ = chave
    guardado = _upload_guardado()
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = cliente.get(
        f"/api/v1/upload/{guardado.job_id}", headers={"Authorization": f"Bearer {_token(privada)}"}
    )

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "processing"
    assert corpo["progress"] == {
        "total": 6,
        "pending": 1,
        "read": 3,
        "failed": 0,
        "ignored": 2,
        "over_limit": 0,
        "percent": 50,
    }
    assert corpo["estimated_cost_usd"] == pytest.approx(0.0324)
    assert corpo["bets_processed"] == 3


def test_an_upload_with_no_bills_yet_shows_no_progress(monkeypatch, chave) -> None:
    privada, _ = chave
    guardado = _upload_guardado(status="pending")
    banco = _banco(upload=guardado)

    class SemBilhetes:
        async def count_enviados_desde(self, session, usuario_id, desde):
            return 0

        async def count_by_estado(self, session, usuario_id, upload_id):
            return {}

    cliente = _cliente(monkeypatch, chave, banco)
    monkeypatch.setattr(rota, "UploadBilheteRepo", SemBilhetes)

    resposta = cliente.get(
        f"/api/v1/upload/{guardado.job_id}", headers={"Authorization": f"Bearer {_token(privada)}"}
    )

    # Um envio recém-aceito não está pronto: 0%, não 100%.
    assert resposta.json()["progress"] == {
        "total": 0,
        "pending": 0,
        "read": 0,
        "failed": 0,
        "ignored": 0,
        "over_limit": 0,
        "percent": 0,
    }


def test_the_status_of_someone_elses_upload_is_not_found(monkeypatch, chave) -> None:
    privada, _ = chave
    guardado = _upload_guardado(usuario_id=99)
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = cliente.get(
        f"/api/v1/upload/{guardado.job_id}", headers={"Authorization": f"Bearer {_token(privada)}"}
    )

    assert resposta.status_code == 404


def _avisar(cliente, job_id, segredo=..., **corpo):
    payload = {
        "job_id": str(job_id),
        "status": "completed",
        "bets_processed": 4,
        "bets_failed": 1,
        "cost_usd": 0.0216,
        **corpo,
    }
    # Nunca um segredo fixo: o CI do autor define o dele, e um literal passaria aqui e falharia lá.
    combinado = get_settings().UPLOAD_WEBHOOK_SECRET if segredo is ... else segredo
    headers = {} if combinado is None else {rota.WEBHOOK_HEADER: combinado}
    return cliente.post(rota.WEBHOOK_PATH, json=payload, headers=headers)


def test_the_worker_closes_the_upload_through_the_webhook(monkeypatch, chave) -> None:
    guardado = _upload_guardado()
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = _avisar(cliente, guardado.job_id)

    assert resposta.status_code == 204
    assert banco.fechados == [(guardado.job_id, "completed", 4, 1, Decimal("0.021600"))]
    assert any("app.upload_job_id" in sql for sql, _ in banco.sql)


def test_the_webhook_needs_the_shared_secret(monkeypatch, chave) -> None:
    guardado = _upload_guardado()
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    sem_segredo = _avisar(cliente, guardado.job_id, segredo=None)
    errado = _avisar(cliente, guardado.job_id, segredo="outro")

    assert (sem_segredo.status_code, errado.status_code) == (403, 403)
    assert banco.fechados == []


def test_a_server_without_a_webhook_secret_refuses_the_notice(monkeypatch, chave) -> None:
    guardado = _upload_guardado()
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)
    monkeypatch.setenv("UPLOAD_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    try:
        resposta = _avisar(cliente, guardado.job_id, segredo="qualquer")
    finally:
        get_settings.cache_clear()

    assert resposta.status_code == 503


def test_the_webhook_of_an_unknown_job_is_not_found(monkeypatch, chave) -> None:
    banco = _banco(upload=_upload_guardado())
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = _avisar(cliente, uuid4())

    assert resposta.status_code == 404
    assert banco.fechados == []


def test_the_webhook_does_not_close_an_upload_that_never_started(monkeypatch, chave) -> None:
    guardado = _upload_guardado(status="pending")
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    assert _avisar(cliente, guardado.job_id).status_code == 404


def test_the_webhook_path_is_public_and_hidden_from_the_docs() -> None:
    assert rota.WEBHOOK_PATH in auth_middleware.PUBLIC_PATHS
    caminhos = {
        caminho for caminho in main.app.openapi().get("paths", {}) if caminho == rota.WEBHOOK_PATH
    }
    assert caminhos == set()


def test_a_notice_with_a_status_the_engine_does_not_know_is_refused(monkeypatch, chave) -> None:
    guardado = _upload_guardado()
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    resposta = _avisar(cliente, guardado.job_id, status="processing")

    assert resposta.status_code == 422
    assert banco.fechados == []


def test_the_upload_writes_to_the_primary_and_the_status_reads_after_it(monkeypatch, chave) -> None:
    privada, _ = chave
    guardado = _upload_guardado()
    banco = _banco(upload=guardado)
    cliente = _cliente(monkeypatch, chave, banco)

    _enviar(cliente, privada)
    cliente.get(
        f"/api/v1/upload/{guardado.job_id}", headers={"Authorization": f"Bearer {_token(privada)}"}
    )

    # O roteador da #25 manda POST ao primário e, por 5 s, as leituras da mesma pessoa também.
    assert banco.enfileirados
    assert banco.escolhas == [True, True]
