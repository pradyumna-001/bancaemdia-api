"""Resolve media only through tenant-owned, persisted bet provenance."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import String, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.config import get_settings
from bancaemdia.domain.foto_aposta import StoredPhoto
from bancaemdia.domain.registros import Aposta
from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES


class ApostaFotoRepo:
    async def get(self, session: AsyncSession, user_id: int, bet: Aposta) -> StoredPhoto | None:
        if bet.usuario_id != user_id:
            return None
        if bet.origem == "telegram_bot":
            # Bot evidence is private and retention-bound. Never fall back to a global hash.
            return await self._bot(session, user_id, bet)
        if bet.origem == "print" and (bet.chave or "").startswith("web_print:"):
            from bancaemdia.models.web_print import PrintAsset
            from bancaemdia.services.web_print import photo

            try:
                prefix, identity, coupon = (bet.chave or "").split(":")
                asset_id = UUID(identity)
                if prefix != "web_print" or bet.chave != f"web_print:{asset_id}:{int(coupon)}":
                    return None
            except (ValueError, TypeError):
                return None
            asset = await session.scalar(
                select(PrintAsset).where(
                    PrintAsset.usuario_id == user_id,
                    PrintAsset.id == asset_id,
                    PrintAsset.digest == bet.midia_hash,
                )
            )
            if asset is None or not any(
                result.get("bet_key") == bet.chave for result in asset.results or []
            ):
                return None
            return await photo(session, user_id, asset_id)
        digest = bet.midia_hash
        if digest is None:
            references = list(
                (
                    await session.scalars(
                        select(models.RevisaoPendente.midia_hash)
                        .where(
                            models.RevisaoPendente.usuario_id == user_id,
                            models.RevisaoPendente.extracao_bruta["aposta_chave"].astext
                            == bet.chave,
                            models.RevisaoPendente.midia_hash.is_not(None),
                        )
                        .distinct()
                        .limit(2)
                    )
                ).all()
            )
            # Several reviews of the same original are fine; distinct originals are ambiguous.
            if len(references) != 1:
                return None
            digest = references[0]
        length = func.octet_length(models.MidiaArquivo.conteudo)
        row = (
            await session.execute(
                select(
                    models.Midia.hash,
                    models.Midia.tipo,
                    case((length <= MAX_PHOTO_BYTES, models.MidiaArquivo.conteudo)),
                    models.MidiaArquivo.s3_key,
                    length,
                )
                .join(models.MidiaArquivo, models.MidiaArquivo.hash == models.Midia.hash)
                .where(models.Midia.hash == digest)
            )
        ).one_or_none()
        return None if row is None else StoredPhoto(row[0], row[1], row[2], row[3], size=row[4])

    async def _bot(self, session: AsyncSession, user_id: int, bet: Aposta) -> StoredPhoto | None:
        if bet.chave is None:
            return None
        try:
            draft_id = UUID(bet.chave.removeprefix("telegram_draft:"))
        except ValueError:
            return None
        if bet.chave != f"telegram_draft:{draft_id}":
            return None
        media, draft, event = models.TelegramMedia, models.RascunhoAposta, models.Evento
        encrypted_limit = ((MAX_PHOTO_BYTES + 2) // 3) * 4 + 256
        rows = (
            await session.execute(
                select(
                    media.content_hash,
                    media.mime,
                    case((
                        func.octet_length(media.content_ciphertext) <= encrypted_limit,
                        media.content_ciphertext,
                    )),
                )
                .join(draft, (draft.id == media.draft_id) & (draft.usuario_id == media.usuario_id))
                .join(
                    event,
                    (event.usuario_id == draft.usuario_id)
                    & (event.payload_json["telegram_draft_id"].astext == cast(draft.id, String)),
                )
                .where(
                    media.usuario_id == user_id,
                    media.draft_id == draft_id,
                    draft.status == "CONFIRMED",
                    draft.purged_at.is_(None),
                    draft.closed_at + timedelta(days=get_settings().TELEGRAM_MEDIA_RETENTION_DAYS)
                    > func.clock_timestamp(),
                    draft.media_hash == media.content_hash,
                    event.tipo == "APOSTA_CRIADA",
                    event.aposta_chave == bet.chave,
                    *([media.content_hash == bet.midia_hash] if bet.midia_hash is not None else []),
                )
                .limit(2)
            )
        ).all()
        if len(rows) != 1:
            return None
        return StoredPhoto(rows[0][0], rows[0][1], rows[0][2], encrypted=True)
