"""One-time pairing and independently revocable least-privilege credentials."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.models.coleta_instalacao import ColetaInstalacao, ColetaPairingCode
from bancaemdia.models.usuario import Usuario
from bancaemdia.repositories.coleta_instalacao import ColetaInstalacaoRepo, owner_scope


def digest(value: str, purpose: str = "token") -> str:
    material = value if purpose == "token" else f"{purpose}:{value}"
    return hmac.new(
        get_settings().COLETA_TOKEN_SECRET.encode(), material.encode(), hashlib.sha256
    ).hexdigest()


def audit(action: str, *, instalacao_id: int | None = None) -> None:
    # Never record label, opaque client ID, hash, prefix, IP, request body or credentials.
    structlog.get_logger(__name__).info(
        "collection_credential", action=action, instalacao_id=instalacao_id
    )


async def admit(session: AsyncSession, *, action: str, identity: str) -> None:
    settings = get_settings()
    maximum = (
        settings.COLETA_PAIRING_CREATE_LIMIT
        if action == "create"
        else settings.COLETA_PAIRING_EXCHANGE_LIMIT
    )
    accepted = True
    try:
        for key, limit in (
            (f"{action}:global", settings.COLETA_PAIRING_GLOBAL_LIMIT),
            (f"{action}:{identity}", maximum),
        ):
            accepted = bool(
                await session.scalar(
                    text("SELECT coleta_pairing_limit(:key,:maximum,:seconds)"),
                    {
                        "key": digest(key, "quota"),
                        "maximum": limit,
                        "seconds": settings.COLETA_PAIRING_WINDOW_SECONDS,
                    },
                )
            )
            if not accepted:
                break
        # Count rejected/invalid exchanges too, independently of their later rollback.
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        audit("limit_unavailable")
        raise HTTPException(503, "Pairing temporarily unavailable") from None
    if not accepted:
        audit("limited")
        raise HTTPException(
            429,
            "Pairing rate limit",
            headers={"Retry-After": str(settings.COLETA_PAIRING_WINDOW_SECONDS)},
        )


async def create_code(session: AsyncSession, usuario_id: int) -> tuple[str, datetime]:
    await admit(session, action="create", identity=str(usuario_id))
    await owner_scope(session, usuario_id)
    now = datetime.now(UTC)
    # Expired/consumed challenges are disposable. Bounded owner cleanup on issuance.
    expired = select(ColetaPairingCode.id).where(ColetaPairingCode.expira_em < now).limit(100)
    await session.execute(delete(ColetaPairingCode).where(ColetaPairingCode.id.in_(expired)))
    code = "cpc_" + secrets.token_urlsafe(24)
    expiry = now + timedelta(seconds=get_settings().COLETA_PAIRING_TTL_SECONDS)
    session.add(
        ColetaPairingCode(
            usuario_id=usuario_id,
            code_hash=digest(code, "pairing"),
            criado_em=now,
            expira_em=expiry,
        )
    )
    await session.commit()
    audit("code_created")
    return code, expiry


async def exchange(
    session: AsyncSession, *, code: str, public_id: UUID, label: str | None, identity: str
) -> tuple[int, str]:
    await admit(session, action="exchange", identity=identity)
    hashed = digest(code, "pairing")
    await session.execute(
        text("SELECT set_config('app.coleta_pairing_hash', :hash, true)"), {"hash": hashed}
    )
    owner = await session.scalar(
        select(ColetaPairingCode.usuario_id).where(ColetaPairingCode.code_hash == hashed)
    )
    if owner is None:
        audit("exchange_rejected")
        raise HTTPException(400, "Invalid or unavailable pairing code")
    await owner_scope(session, owner)
    challenge = await session.scalar(
        select(ColetaPairingCode).where(ColetaPairingCode.code_hash == hashed).with_for_update()
    )
    now = datetime.now(UTC)
    active = await session.scalar(select(Usuario.ativo).where(Usuario.id == owner))
    if (
        challenge is None
        or challenge.consumido_em is not None
        or challenge.expira_em <= now
        or not active
    ):
        audit("exchange_rejected")
        raise HTTPException(400, "Invalid or unavailable pairing code")
    token = "cti_" + secrets.token_urlsafe(32)
    statement = insert(ColetaInstalacao).values(
        usuario_id=owner,
        instalacao_publica_id=public_id,
        nome_dispositivo=label,
        token_hash=digest(token),
        token_prefixo=token[:12],
        pareado_em=now,
    )
    # Concurrent re-pairing of the same installation serializes on its unique key.
    result = await session.scalar(
        statement.on_conflict_do_update(
            constraint="uq_coleta_instalacao_owner_public",
            set_={
                "nome_dispositivo": label,
                "token_hash": digest(token),
                "token_prefixo": token[:12],
                "pareado_em": now,
                "rotacionado_em": now,
                "revogado_em": None,
                "expira_em": None,
            },
        ).returning(ColetaInstalacao.id)
    )
    assert result is not None
    challenge.consumido_em = now
    await session.commit()
    audit("paired", instalacao_id=result)
    return result, token


async def rotate_or_revoke(
    session: AsyncSession, usuario_id: int, instalacao_id: int, *, revoke: bool
) -> str | None:
    row = await ColetaInstalacaoRepo().owned(session, usuario_id, instalacao_id)
    if row is None or (not revoke and row.revogado_em is not None):
        raise HTTPException(404, "Installation unavailable")
    token = None if revoke else "cti_" + secrets.token_urlsafe(32)
    row.token_hash = None if token is None else digest(token)
    row.token_prefixo = None if token is None else token[:12]
    if revoke:
        row.revogado_em = row.revogado_em or datetime.now(UTC)
    else:
        row.rotacionado_em = datetime.now(UTC)
    await session.commit()
    audit("revoked" if revoke else "rotated", instalacao_id=instalacao_id)
    return token
