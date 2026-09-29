from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


class TitularRepo:
    async def create(self, session: AsyncSession, usuario_id: int, nome: str) -> models.Titular:
        return (
            await session.execute(
                insert(models.Titular)
                .values(usuario_id=usuario_id, nome=nome.strip())
                .returning(models.Titular)
            )
        ).scalar_one()

    async def get_by_id(
        self, session: AsyncSession, usuario_id: int, titular_id: int
    ) -> models.Titular | None:
        return (
            await session.execute(
                select(models.Titular).where(
                    models.Titular.usuario_id == usuario_id,
                    models.Titular.id == titular_id,
                )
            )
        ).scalar_one_or_none()

    async def archive(
        self, session: AsyncSession, usuario_id: int, titular_id: int
    ) -> models.Titular | None:
        return (
            await session.execute(
                update(models.Titular)
                .where(
                    models.Titular.usuario_id == usuario_id,
                    models.Titular.id == titular_id,
                )
                .values(arquivado=True)
                .returning(models.Titular)
            )
        ).scalar_one_or_none()

    async def update_name(
        self, session: AsyncSession, usuario_id: int, titular_id: int, nome: str
    ) -> models.Titular | None:
        return (
            await session.execute(
                update(models.Titular)
                .where(
                    models.Titular.usuario_id == usuario_id,
                    models.Titular.id == titular_id,
                    models.Titular.arquivado.is_(False),
                )
                .values(nome=nome.strip())
                .returning(models.Titular)
            )
        ).scalar_one_or_none()

    async def list_page(
        self,
        session: AsyncSession,
        usuario_id: int,
        *,
        search: str | None,
        include_archived: bool,
        page: int,
        page_size: int,
    ) -> tuple[list[models.Titular], int]:
        stmt = select(models.Titular).where(models.Titular.usuario_id == usuario_id)
        if not include_archived:
            stmt = stmt.where(models.Titular.arquivado.is_(False))
        if search:
            stmt = stmt.where(models.Titular.nome.icontains(search, autoescape=True))
        total = int((await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0)
        rows = list(
            (
                await session.execute(
                    stmt
                    .order_by(models.Titular.nome, models.Titular.id)
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                )
            ).scalars()
        )
        return rows, total

    async def list_all(self, session: AsyncSession, usuario_id: int) -> list[models.Titular]:
        return list(
            (
                await session.execute(
                    select(models.Titular)
                    .where(models.Titular.usuario_id == usuario_id)
                    .order_by(models.Titular.nome, models.Titular.id)
                )
            ).scalars()
        )
