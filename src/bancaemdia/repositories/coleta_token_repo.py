from datetime import datetime

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


class ColetaTokenRepo:
    async def get_usuario_id_by_hash(self, session: AsyncSession, token_hash: str) -> int | None:
        await session.execute(
            text("SELECT set_config('app.coleta_token_hash', :hash, true)"), {"hash": token_hash}
        )
        stmt = select(models.ColetaToken.usuario_id).where(
            models.ColetaToken.token_hash == token_hash,
            models.ColetaToken.ativo.is_(True),
            or_(models.ColetaToken.expira_em.is_(None), models.ColetaToken.expira_em > func.now()),
        )
        usuario_id: int | None = (await session.execute(stmt)).scalar_one_or_none()
        return usuario_id

    async def create(
        self,
        session: AsyncSession,
        usuario_id: int,
        token_hash: str,
        expira_em: datetime | None = None,
    ) -> int:
        stmt = (
            insert(models.ColetaToken)
            .values(usuario_id=usuario_id, token_hash=token_hash, expira_em=expira_em)
            .returning(models.ColetaToken.id)
        )
        token_id: int = (await session.execute(stmt)).scalar_one()
        return token_id

    async def deactivate_by_usuario(self, session: AsyncSession, usuario_id: int) -> int:
        stmt = (
            update(models.ColetaToken)
            .where(models.ColetaToken.usuario_id == usuario_id, models.ColetaToken.ativo.is_(True))
            .values(ativo=False)
            .returning(models.ColetaToken.id)
        )
        return len(list((await session.execute(stmt)).scalars()))
