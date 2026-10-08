from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.models.coleta_instalacao import ColetaInstalacao
from bancaemdia.models.usuario import Usuario


@dataclass(frozen=True)
class CollectionIdentity:
    usuario_id: int
    instalacao_id: int


async def owner_scope(session: AsyncSession, usuario_id: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario_id)}
    )


class ColetaInstalacaoRepo:
    async def authenticate(
        self, session: AsyncSession, token_hash: str
    ) -> CollectionIdentity | None:
        await session.execute(
            text("SELECT set_config('app.coleta_token_hash', :hash, true)"), {"hash": token_hash}
        )
        owner = await session.scalar(
            select(ColetaInstalacao.usuario_id).where(ColetaInstalacao.token_hash == token_hash)
        )
        if owner is None:
            return None
        await owner_scope(session, owner)
        # Updating last-use acquires the credential's row lock until commit, just
        # like the former SELECT FOR UPDATE + flush. Check the active owner in
        # that same statement; rotation/revocation still waits for accepted work.
        row = await session.scalar(
            update(ColetaInstalacao)
            .where(
                ColetaInstalacao.token_hash == token_hash,
                ColetaInstalacao.revogado_em.is_(None),
                or_(ColetaInstalacao.expira_em.is_(None), ColetaInstalacao.expira_em > func.now()),
                Usuario.id == ColetaInstalacao.usuario_id,
                Usuario.ativo,
            )
            .values(ultimo_uso_em=datetime.now(UTC))
            .returning(ColetaInstalacao)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        if row is None:
            return None
        return CollectionIdentity(owner, row.id)

    async def owned(
        self, session: AsyncSession, usuario_id: int, instalacao_id: int
    ) -> ColetaInstalacao | None:
        row: ColetaInstalacao | None = await session.scalar(
            select(ColetaInstalacao)
            .where(ColetaInstalacao.id == instalacao_id, ColetaInstalacao.usuario_id == usuario_id)
            .with_for_update()
        )

        return row
