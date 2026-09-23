from datetime import datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.config import get_settings
from bancaemdia.storage import midia_s3


class Salvamento(StrEnum):
    NOVA = "NOVA"
    ALTERADA = "ALTERADA"
    IGUAL = "IGUAL"
    ANTIGA = "ANTIGA"


def _e_mais_antiga(guardada: datetime | None, chegando: datetime | None) -> bool:
    # A defesa contra reimportar um export velho por cima do novo é a hora de edição do próprio
    # Telegram: sem ela do lado guardado não há o que comparar; sem ela do lado que chega, o
    # guardado já foi editado e este é retrato velho.
    if guardada is None:
        return False
    if chegando is None:
        return True
    return chegando < guardada


class MensagemRepo:
    async def save(
        self,
        session: AsyncSession,
        chat_id: int,
        message_id: int,
        data: datetime,
        autor: str | None,
        texto: str,
        editada_em: datetime | None = None,
        midia_hash: str | None = None,
    ) -> tuple[int, Salvamento]:
        stmt = select(models.Mensagem).where(
            models.Mensagem.chat_id == chat_id, models.Mensagem.message_id == message_id
        )
        atual = (await session.execute(stmt)).scalar_one_or_none()
        if atual is None:
            # Dois exports do mesmo chat podem ser lidos ao mesmo tempo: sem o ON CONFLICT, o
            # segundo quebraria na chave única entre a leitura e a escrita.
            obj = (
                await session.execute(
                    insert(models.Mensagem)
                    .values(
                        chat_id=chat_id,
                        message_id=message_id,
                        data=data,
                        autor_bruto=autor,
                        texto=texto,
                        editada_em=editada_em,
                        midia_hash=midia_hash,
                    )
                    .on_conflict_do_nothing(constraint="uq_mensagens_chat_message")
                    .returning(models.Mensagem)
                )
            ).scalar_one_or_none()
            if obj is not None:
                await self._gravar_versao(session, obj.id, texto)
                return obj.id, Salvamento.NOVA
            atual = (await session.execute(stmt)).scalar_one()

        alterada = False
        # A foto que faltava entra mesmo com o texto igual: o primeiro export do dono veio sem
        # fotos por um defeito, e reenviar o mesmo arquivo não adiantava nada (19/08/2026). Só
        # PREENCHE o que faltava — export sem foto não apaga a foto que já entrou.
        if midia_hash and not atual.midia_hash:
            await session.execute(
                update(models.Mensagem)
                .where(models.Mensagem.id == atual.id)
                .values(midia_hash=midia_hash)
            )
            alterada = True

        if atual.texto == texto:
            return atual.id, Salvamento.ALTERADA if alterada else Salvamento.IGUAL
        if _e_mais_antiga(atual.editada_em, editada_em):
            return atual.id, Salvamento.ANTIGA

        await session.execute(
            update(models.Mensagem)
            .where(models.Mensagem.id == atual.id)
            .values(texto=texto, editada_em=editada_em, versao_atual=atual.versao_atual + 1)
        )
        await self._gravar_versao(session, atual.id, texto)
        return atual.id, Salvamento.ALTERADA

    async def _gravar_versao(self, session: AsyncSession, mensagem_id: int, texto: str) -> None:
        await session.execute(
            insert(models.MensagemVersao).values(mensagem_id=mensagem_id, texto=texto)
        )

    async def get_by_chat_message(
        self, session: AsyncSession, chat_id: int, message_id: int
    ) -> models.Mensagem | None:
        stmt = select(models.Mensagem).where(
            models.Mensagem.chat_id == chat_id, models.Mensagem.message_id == message_id
        )
        return (await session.execute(stmt)).scalar_one_or_none()


class MidiaRepo:
    async def upsert_idempotent(
        self,
        session: AsyncSession,
        hash_: str,
        tipo: str,
        bytes_: int,
        mensagem_id: int | None = None,
    ) -> None:
        # A mesma foto pode chegar em mensagens diferentes (reenvio, dois canais): a primeira
        # mensagem que a trouxe fica registrada, e a segunda não rouba o vínculo.
        await session.execute(
            insert(models.Midia)
            .values(hash=hash_, tipo=tipo, bytes=bytes_, mensagem_id=mensagem_id)
            .on_conflict_do_nothing(index_elements=["hash"])
        )


class MidiaArquivoRepo:
    async def upsert_idempotent(self, session: AsyncSession, hash_: str, conteudo: bytes) -> None:
        bucket = get_settings().S3_UPLOAD_BUCKET
        key = None if bucket is None else await midia_s3.upload(bucket, hash_, conteudo)
        stmt = insert(models.MidiaArquivo).values(hash=hash_, conteudo=conteudo, s3_key=key)
        if key is None:
            stmt = stmt.on_conflict_do_nothing(index_elements=["hash"])
        else:
            stmt = stmt.on_conflict_do_update(index_elements=["hash"], set_={"s3_key": key})
        await session.execute(stmt)

    async def keys_for_hashes(
        self, session: AsyncSession, hashes: list[str]
    ) -> dict[str, tuple[str, str]]:
        if not hashes:
            return {}
        stmt = (
            select(models.MidiaArquivo.hash, models.MidiaArquivo.s3_key, models.Midia.tipo)
            .join(models.Midia, models.Midia.hash == models.MidiaArquivo.hash)
            .where(models.MidiaArquivo.hash.in_(hashes), models.MidiaArquivo.s3_key.is_not(None))
        )
        return {
            str(hash_): (str(key), str(tipo))
            for hash_, key, tipo in (await session.execute(stmt)).all()
        }

    async def get_by_hash(self, session: AsyncSession, hash_: str) -> bytes | None:
        stmt = select(models.MidiaArquivo.conteudo).where(models.MidiaArquivo.hash == hash_)
        return (await session.execute(stmt)).scalar_one_or_none()
