from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from bancaemdia import models
from bancaemdia.repositories.mensagem_repo import (
    MensagemRepo,
    MidiaArquivoRepo,
    MidiaRepo,
    Salvamento,
    _e_mais_antiga,
)
from bancaemdia.repositories.upload_repo import UploadArquivoRepo, UploadBilheteRepo, UploadRepo

AGORA = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
ONTEM = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
CHAT = 555


class _Result:
    def __init__(self, objs: list[Any]) -> None:
        self.objs = objs

    def scalar_one_or_none(self) -> Any:
        return self.objs[0] if self.objs else None

    def scalar_one(self) -> Any:
        return self.objs[0]

    def scalars(self) -> Any:
        return iter(self.objs)

    def all(self) -> list[Any]:
        return self.objs

    def one(self) -> Any:
        return self.objs[0]


class _Session:
    def __init__(self, *objs: Any) -> None:
        self.objs = list(objs)
        self.statements: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.statements.append(statement)
        return _Result(self.objs)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def _upload(status: str = "pending", **extra: Any) -> models.Upload:
    return models.Upload(
        id=1,
        job_id=uuid4(),
        usuario_id=7,
        filename="ChatExport.zip",
        chat_id=CHAT,
        status=status,
        total_messages=3,
        estimated_bets=2,
        estimated_cost_usd=Decimal("0.0108"),
        bets_processed=0,
        bets_failed=0,
        cost_usd=Decimal(0),
        erro=None,
        criado_em=AGORA,
        concluido_em=None,
        **extra,
    )


def _bilhete(**extra: Any) -> models.UploadBilhete:
    return models.UploadBilhete(
        id=3,
        upload_id=1,
        usuario_id=7,
        chat_id=CHAT,
        message_id=9,
        midia_hash="h1",
        estado="PENDENTE",
        apostas=0,
        custo_usd=Decimal(0),
        enfileirado_em=None,
        criado_em=AGORA,
        **extra,
    )


async def test_an_upload_is_read_back_by_its_job_and_its_owner() -> None:
    guardado = _upload()
    session = _Session(guardado)

    lido = await UploadRepo().get_by_job_id(session, 7, guardado.job_id)

    assert lido is not None and lido.job_id == guardado.job_id
    sql = _sql(session.statements[0])
    assert "WHERE uploads.usuario_id = " in sql and "uploads.job_id = " in sql


async def test_the_last_accepted_upload_ignores_the_failed_ones() -> None:
    session = _Session(AGORA)

    quando = await UploadRepo().get_ultimo_aceito_em(session, 7)

    assert quando == AGORA
    sql = _sql(session.statements[0])
    assert "max(uploads.criado_em)" in sql
    assert "uploads.status !=" in sql


async def test_finishing_an_upload_needs_it_to_have_started() -> None:
    guardado = _upload(status="processing")
    session = _Session(guardado.id)

    await UploadRepo().finish_by_job_id(
        session, guardado.job_id, "completed", 4, 1, Decimal("0.0216")
    )

    sql = _sql(session.statements[0])
    assert "UPDATE uploads SET" in sql
    assert "uploads.status != " in sql
    assert "coalesce(uploads.concluido_em, now())" in sql
    assert "RETURNING uploads.id" in sql


async def test_closing_an_upload_stamps_the_moment_only_when_it_ends() -> None:
    session = _Session()

    await UploadRepo().set_status(session, 7, 1, "processing")
    await UploadRepo().set_status(session, 7, 1, "failed", "a fila caiu")

    assert "concluido_em" not in _sql(session.statements[0])
    fechado = _sql(session.statements[1])
    assert "concluido_em=now()" in fechado.replace(" ", "")
    assert _params(session.statements[1])["erro"] == "a fila caiu"


async def test_bills_are_inserted_once_per_message() -> None:
    session = _Session()

    await UploadBilheteRepo().insert_many_idempotent(
        session, [{"upload_id": 1, "usuario_id": 7, "chat_id": CHAT, "message_id": 9}]
    )

    sql = _sql(session.statements[0])
    assert "INSERT INTO upload_bilhetes" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_upload_bilhetes_mensagem DO NOTHING" in sql


async def test_an_empty_list_of_bills_touches_the_database_at_all() -> None:
    session = _Session()

    await UploadBilheteRepo().insert_many_idempotent(session, [])

    assert session.statements == []


async def test_the_daily_count_only_sees_what_went_to_the_ai() -> None:
    session = _Session(3)

    quantos = await UploadBilheteRepo().count_enviados_desde(session, 7, ONTEM)

    assert quantos == 3
    sql = _sql(session.statements[0])
    assert "upload_bilhetes.estado IN " in sql
    assert _params(session.statements[0])["criado_em_1"] == ONTEM


async def test_a_bill_only_leaves_pending_once() -> None:
    session = _Session(3)

    await UploadBilheteRepo().finish(session, 7, 1, CHAT, 9, "LIDO", 2, Decimal("0.004"))

    sql = _sql(session.statements[0])
    assert "UPDATE upload_bilhetes SET" in sql
    assert "upload_bilhetes.estado = " in sql
    assert "RETURNING upload_bilhetes.id" in sql
    assert _params(session.statements[0])["estado"] == "LIDO"


async def test_the_bills_waiting_to_be_queued_are_the_pending_ones_never_sent() -> None:
    session = _Session(_bilhete())

    [bilhete] = await UploadBilheteRepo().list_pendentes(session, 7, 1)

    assert bilhete.message_id == 9
    sql = _sql(session.statements[0])
    assert "upload_bilhetes.enfileirado_em IS NULL" in sql
    assert "ORDER BY upload_bilhetes.id" in sql


async def test_the_states_of_an_upload_come_counted() -> None:
    session = _Session(("LIDO", 2), ("PENDENTE", 1))

    por_estado = await UploadBilheteRepo().count_by_estado(session, 7, 1)

    assert por_estado == {"LIDO": 2, "PENDENTE": 1}
    assert "GROUP BY upload_bilhetes.estado" in _sql(session.statements[0])


async def test_the_result_of_an_upload_adds_bets_failures_and_cost() -> None:
    session = _Session((4, 1, Decimal("0.0216")))

    assert await UploadBilheteRepo().somar_resultado(session, 7, 1) == (4, 1, Decimal("0.0216"))
    sql = _sql(session.statements[0])
    assert "sum(upload_bilhetes.apostas)" in sql
    assert "FILTER (WHERE upload_bilhetes.estado = " in sql


async def test_the_uploaded_file_waits_in_its_own_row_and_is_deleted_after_use() -> None:
    session = _Session(b"PK")

    await UploadArquivoRepo().create(session, 7, 1, b"PK")
    await UploadArquivoRepo().get_by_upload_id(session, 7, 1)
    await UploadArquivoRepo().delete_by_upload_id(session, 7, 1)

    assert "INSERT INTO upload_arquivos" in _sql(session.statements[0])
    assert "SELECT upload_arquivos.conteudo" in _sql(session.statements[1])
    assert "DELETE FROM upload_arquivos" in _sql(session.statements[2])


async def test_a_message_never_seen_is_saved_with_its_first_version() -> None:
    class Session(_Session):
        def __init__(self) -> None:
            super().__init__()
            self.respostas = [[], [models.Mensagem(id=5)], []]

        async def execute(self, statement: Any, params: Any = None) -> _Result:
            self.statements.append(statement)
            return _Result(self.respostas.pop(0))

    session = Session()

    mensagem_id, resultado = await MensagemRepo().save(session, CHAT, 9, AGORA, "girafalles", "1u")

    assert (mensagem_id, resultado) == (5, Salvamento.NOVA)
    assert "INSERT INTO mensagens" in _sql(session.statements[1])
    assert "INSERT INTO mensagem_versoes" in _sql(session.statements[2])


async def test_a_message_written_by_another_export_in_between_is_not_a_crash() -> None:
    guardada = models.Mensagem(id=5, texto="1u", editada_em=None, midia_hash=None, versao_atual=1)

    class Session(_Session):
        def __init__(self) -> None:
            super().__init__()
            # SELECT vazio, INSERT que bate na chave unica, e o SELECT de novo achando a linha.
            self.respostas = [[], [], [guardada]]

        async def execute(self, statement: Any, params: Any = None) -> _Result:
            self.statements.append(statement)
            return _Result(self.respostas.pop(0))

    session = Session()

    mensagem_id, resultado = await MensagemRepo().save(session, CHAT, 9, AGORA, "girafalles", "1u")

    assert (mensagem_id, resultado) == (5, Salvamento.IGUAL)
    assert "ON CONFLICT ON CONSTRAINT uq_mensagens_chat_message DO NOTHING" in _sql(
        session.statements[1]
    )


async def test_a_message_that_did_not_change_is_left_alone() -> None:
    guardada = models.Mensagem(id=5, texto="1u", editada_em=None, midia_hash="h1", versao_atual=1)
    session = _Session(guardada)

    mensagem_id, resultado = await MensagemRepo().save(
        session, CHAT, 9, AGORA, "girafalles", "1u", midia_hash="h1"
    )

    assert (mensagem_id, resultado) == (5, Salvamento.IGUAL)
    assert len(session.statements) == 1


def test_an_older_export_does_not_undo_an_edit() -> None:
    assert _e_mais_antiga(guardada=AGORA, chegando=ONTEM)
    assert _e_mais_antiga(guardada=AGORA, chegando=None)
    assert not _e_mais_antiga(guardada=None, chegando=ONTEM)
    assert not _e_mais_antiga(guardada=ONTEM, chegando=AGORA)


async def test_a_photo_is_stored_once_and_read_back_by_its_fingerprint() -> None:
    session = _Session(b"JPEG")

    await MidiaRepo().upsert_idempotent(session, "h1", "image/jpeg", 4, 5)
    await MidiaArquivoRepo().upsert_idempotent(session, "h1", b"JPEG")
    dados = await MidiaArquivoRepo().get_by_hash(session, "h1")

    assert dados == b"JPEG"
    assert "ON CONFLICT (hash) DO NOTHING" in _sql(session.statements[0])
    assert "ON CONFLICT (hash) DO NOTHING" in _sql(session.statements[1])
    assert "SELECT midia_arquivos.conteudo" in _sql(session.statements[2])
