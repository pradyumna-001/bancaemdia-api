from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Upload, UploadBilhete
from bancaemdia.repositories.base import colunas

ESPERANDO = ("PENDENTE", "LIDO", "FALHOU")
LOTE_DE_BILHETES = 1000


class UploadRepo:
    async def create(self, session: AsyncSession, dados: dict[str, object]) -> Upload:
        obj = (
            await session.execute(insert(models.Upload).values(**dados).returning(models.Upload))
        ).scalar_one()
        return Upload(**colunas(obj))

    async def get_by_job_id(
        self, session: AsyncSession, usuario_id: int, job_id: UUID
    ) -> Upload | None:
        stmt = select(models.Upload).where(
            models.Upload.usuario_id == usuario_id, models.Upload.job_id == job_id
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Upload(**colunas(obj))

    async def get_by_id_for_update(
        self, session: AsyncSession, usuario_id: int, upload_id: int
    ) -> Upload | None:
        stmt = (
            select(models.Upload)
            .where(models.Upload.usuario_id == usuario_id, models.Upload.id == upload_id)
            .with_for_update()
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Upload(**colunas(obj))

    async def get_ultimo_aceito_em(self, session: AsyncSession, usuario_id: int) -> datetime | None:
        # O envio que falhou não conta para o intervalo: um arquivo recusado não pode prender a
        # pessoa por cinco minutos com o arquivo certo na mão.
        stmt = (
            select(func.max(models.Upload.criado_em))
            .where(models.Upload.usuario_id == usuario_id)
            .where(models.Upload.status != "failed")
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def set_status(
        self,
        session: AsyncSession,
        usuario_id: int,
        upload_id: int,
        status: str,
        erro: str | None = None,
    ) -> None:
        valores: dict[str, object] = {"status": status}
        if erro is not None:
            valores["erro"] = erro
        if status in {"completed", "failed"}:
            valores["concluido_em"] = func.now()
        await session.execute(
            update(models.Upload)
            .where(models.Upload.usuario_id == usuario_id, models.Upload.id == upload_id)
            .values(**valores)
        )

    async def set_contagem_do_export(
        self,
        session: AsyncSession,
        usuario_id: int,
        upload_id: int,
        total_messages: int,
        chat_id: int | None,
    ) -> None:
        await session.execute(
            update(models.Upload)
            .where(models.Upload.usuario_id == usuario_id, models.Upload.id == upload_id)
            .values(total_messages=total_messages, chat_id=chat_id)
        )

    async def finish_by_job_id(
        self,
        session: AsyncSession,
        job_id: UUID,
        status: str,
        bets_processed: int,
        bets_failed: int,
        cost_usd: Decimal,
    ) -> int | None:
        # A linha só fecha depois de o trabalhador ter começado: um aviso que chegasse antes do
        # processamento deixaria o envio "concluído" sem nada ter sido lido.
        stmt = (
            update(models.Upload)
            .where(models.Upload.job_id == job_id, models.Upload.status != "pending")
            .values(
                status=status,
                bets_processed=bets_processed,
                bets_failed=bets_failed,
                cost_usd=cost_usd,
                concluido_em=func.coalesce(models.Upload.concluido_em, func.now()),
            )
            .returning(models.Upload.id)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


class UploadBilheteRepo:
    async def insert_many_idempotent(
        self, session: AsyncSession, linhas: list[dict[str, object]]
    ) -> None:
        # Em lotes: um INSERT único passa dos 32.767 parâmetros do protocolo acima de ~5.400 fotos,
        # e um export de um ano inteiro chega lá.
        for inicio in range(0, len(linhas), LOTE_DE_BILHETES):
            fatia = linhas[inicio : inicio + LOTE_DE_BILHETES]
            # A tarefa pode ser reentregue depois de gravar parte dos bilhetes; o que já está lá
            # fica como está, com o estado que a leitura já lhe deu.
            await session.execute(
                insert(models.UploadBilhete)
                .values(fatia)
                .on_conflict_do_nothing(constraint="uq_upload_bilhetes_mensagem")
            )

    async def count_enviados_desde(
        self, session: AsyncSession, usuario_id: int, desde: datetime
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(models.UploadBilhete)
            .where(models.UploadBilhete.usuario_id == usuario_id)
            .where(models.UploadBilhete.criado_em >= desde)
            .where(models.UploadBilhete.estado.in_(ESPERANDO))
        )
        return (await session.execute(stmt)).scalar_one()

    async def list_pendentes(
        self, session: AsyncSession, usuario_id: int, upload_id: int
    ) -> list[UploadBilhete]:
        stmt = (
            select(models.UploadBilhete)
            .where(
                models.UploadBilhete.usuario_id == usuario_id,
                models.UploadBilhete.upload_id == upload_id,
                models.UploadBilhete.estado == "PENDENTE",
                models.UploadBilhete.enfileirado_em.is_(None),
            )
            .order_by(models.UploadBilhete.id)
        )
        return [UploadBilhete(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def mark_enfileirado(
        self, session: AsyncSession, usuario_id: int, bilhete_id: int
    ) -> None:
        await session.execute(
            update(models.UploadBilhete)
            .where(
                models.UploadBilhete.usuario_id == usuario_id,
                models.UploadBilhete.id == bilhete_id,
            )
            .values(enfileirado_em=func.now())
        )

    async def finish(
        self,
        session: AsyncSession,
        usuario_id: int,
        upload_id: int,
        chat_id: int,
        message_id: int,
        estado: str,
        apostas: int = 0,
        custo_usd: Decimal = Decimal(0),
    ) -> int | None:
        # Só sai de PENDENTE: entrega repetida da mesma leitura não soma a mesma aposta duas vezes
        # na conta do envio.
        stmt = (
            update(models.UploadBilhete)
            .where(
                models.UploadBilhete.usuario_id == usuario_id,
                models.UploadBilhete.upload_id == upload_id,
                models.UploadBilhete.chat_id == chat_id,
                models.UploadBilhete.message_id == message_id,
                models.UploadBilhete.estado == "PENDENTE",
            )
            .values(estado=estado, apostas=apostas, custo_usd=custo_usd)
            .returning(models.UploadBilhete.id)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def count_by_estado(
        self, session: AsyncSession, usuario_id: int, upload_id: int
    ) -> dict[str, int]:
        stmt = (
            select(models.UploadBilhete.estado, func.count())
            .where(
                models.UploadBilhete.usuario_id == usuario_id,
                models.UploadBilhete.upload_id == upload_id,
            )
            .group_by(models.UploadBilhete.estado)
        )
        return {estado: int(quantos) for estado, quantos in (await session.execute(stmt)).all()}

    async def somar_resultado(
        self, session: AsyncSession, usuario_id: int, upload_id: int
    ) -> tuple[int, int, Decimal]:
        stmt = select(
            func.coalesce(func.sum(models.UploadBilhete.apostas), 0),
            func.count().filter(models.UploadBilhete.estado == "FALHOU"),
            func.coalesce(func.sum(models.UploadBilhete.custo_usd), 0),
        ).where(
            models.UploadBilhete.usuario_id == usuario_id,
            models.UploadBilhete.upload_id == upload_id,
        )
        apostas, falhas, custo = (await session.execute(stmt)).one()
        return int(apostas), int(falhas), Decimal(custo)


class UploadArquivoRepo:
    async def create(
        self, session: AsyncSession, usuario_id: int, upload_id: int, conteudo: bytes
    ) -> None:
        await session.execute(
            insert(models.UploadArquivo).values(
                usuario_id=usuario_id, upload_id=upload_id, conteudo=conteudo
            )
        )

    async def get_by_upload_id(
        self, session: AsyncSession, usuario_id: int, upload_id: int
    ) -> bytes | None:
        stmt = select(models.UploadArquivo.conteudo).where(
            models.UploadArquivo.usuario_id == usuario_id,
            models.UploadArquivo.upload_id == upload_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def delete_by_upload_id(
        self, session: AsyncSession, usuario_id: int, upload_id: int
    ) -> None:
        await session.execute(
            delete(models.UploadArquivo).where(
                models.UploadArquivo.usuario_id == usuario_id,
                models.UploadArquivo.upload_id == upload_id,
            )
        )
