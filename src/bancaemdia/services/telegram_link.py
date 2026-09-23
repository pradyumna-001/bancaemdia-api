"""Transactional Telegram account-link lifecycle, independent of webhook transport."""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.models import TelegramLink, TelegramLinkCode
from bancaemdia.repositories.telegram_link import TelegramLinkRepo

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_RE = re.compile(r"^/vincular(?:@\w+)? ([23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8})$", re.ASCII)
CODE_LIFETIME = timedelta(minutes=30)
ISSUE_LIMIT = 3
REPO = TelegramLinkRepo()


class CodeRateLimitError(Exception):
    pass


@dataclass(frozen=True)
class IssuedCode:
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class IncomingCommand:
    # The authenticated transport supplies sender_user_id from message.from.id,
    # never forward_origin or a user-supplied field.
    chat_type: str
    sender_user_id: int
    chat_id: int
    text: str


def _digest(kind: str, value: str) -> str:
    secret = get_settings().COLETA_TOKEN_SECRET.encode()
    return hmac.new(secret, f"telegram-{kind}:v1:{value}".encode(), hashlib.sha256).hexdigest()


async def forget_sender_attempts(session: AsyncSession, sender_user_id: int) -> None:
    """Remove keyed abuse state when its owning Banca em Dia account is erased."""
    await REPO.clear_attempts(session, _digest("sender", str(sender_user_id)))


def _now() -> datetime:
    return datetime.now(UTC)


async def issue_code(session: AsyncSession, user_id: int) -> IssuedCode:
    # Serialize issuance and redemption for this account with the same user lock.
    user = await REPO.lock_user(session, user_id)
    if user is None or not user.ativo:
        raise CodeRateLimitError
    now = _now()
    if await REPO.issued_since(session, user_id, now - CODE_LIFETIME) >= ISSUE_LIMIT:
        raise CodeRateLimitError
    await session.execute(
        update(TelegramLinkCode)
        .where(
            TelegramLinkCode.usuario_id == user_id,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    # Roughly 40 bits of randomness; collisions are checked against the unique digest.
    # Regeneration is bounded; a conflicting insert is rolled back only to a savepoint.
    for _ in range(5):
        plain = "".join(secrets.choice(ALPHABET) for _ in range(8))
        digest = _digest("code", plain)
        if await REPO.code_issuer(session, digest) is not None:
            continue
        expires_at = now + CODE_LIFETIME
        try:
            async with session.begin_nested():
                session.add(
                    TelegramLinkCode(
                        usuario_id=user_id, code_hash=digest, issued_at=now, expires_at=expires_at
                    )
                )
                await session.flush()
        except IntegrityError:
            continue
        return IssuedCode(plain, expires_at)
    raise CodeRateLimitError


async def get_link(session: AsyncSession, user_id: int) -> TelegramLink | None:
    return await REPO.active_link(session, user_id)


async def revoke_link(session: AsyncSession, user_id: int) -> bool:
    await REPO.lock_user(session, user_id)
    now = _now()
    await session.execute(
        update(TelegramLinkCode)
        .where(
            TelegramLinkCode.usuario_id == user_id,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    link = await REPO.active_link(session, user_id)
    if link is None:
        return False
    link.revoked_at = now
    await session.flush()
    return True


async def _failed(
    session: AsyncSession, sender_digest: str, outcome: str = "INVALID", *, commit: bool = True
) -> bool:
    await REPO.record_failure(session, sender_digest, outcome)
    if commit:
        await session.commit()
    return False


async def redeem_command(
    session: AsyncSession, command: IncomingCommand, *, commit: bool = True
) -> bool:
    """Consume a code and bind a private sender. False always has the same public wording."""
    if command.sender_user_id <= 0:
        return False
    sender_digest = _digest("sender", str(command.sender_user_id))
    if command.chat_type != "private" or command.chat_id <= 0:
        return await _failed(session, sender_digest, commit=commit)
    # Serializes attempts by one sender across workers. No raw sender ID is logged.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:digest, 0))"),
        {"digest": sender_digest},
    )
    now = _now()
    blocked_until = await REPO.blocked_until(session, sender_digest)
    if blocked_until is not None and blocked_until > now:
        return await _failed(session, sender_digest, "BLOCKED", commit=commit)
    match = CODE_RE.fullmatch(command.text.strip().upper().replace("/VINCULAR", "/vincular", 1))
    if match is None:
        return await _failed(session, sender_digest, commit=commit)
    digest = _digest("code", match.group(1))
    owner = await REPO.code_issuer(session, digest)
    if owner is None:
        return await _failed(session, sender_digest, commit=commit)
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(owner)}
    )
    user = await REPO.lock_user(session, owner)
    if user is None or not user.ativo:
        return await _failed(session, sender_digest, commit=commit)
    code = await REPO.code(session, owner, digest)
    if code is None:
        return await _failed(session, sender_digest, commit=commit)
    if (
        code.consumed_at is not None
        or code.invalidated_at is not None
        or code.expires_at <= now
        or code.failed_attempts >= 5
    ):
        code.failed_attempts = min(5, code.failed_attempts + 1)
        return await _failed(session, sender_digest, commit=commit)
    if await REPO.active_link(session, owner) is not None:
        code.failed_attempts = min(5, code.failed_attempts + 1)
        return await _failed(session, sender_digest, commit=commit)
    # The sender advisory lock also serializes this identity across tenants.
    other_owner = await REPO.active_owner(session, command.sender_user_id, command.chat_id)
    if other_owner is not None:
        code.failed_attempts = min(5, code.failed_attempts + 1)
        return await _failed(session, sender_digest, commit=commit)
    pair = await REPO.pair(session, owner, command.sender_user_id)
    if pair is None:
        session.add(
            TelegramLink(
                usuario_id=owner,
                telegram_user_id=command.sender_user_id,
                telegram_chat_id=command.chat_id,
                linked_at=now,
            )
        )
    else:
        pair.telegram_chat_id = command.chat_id
        pair.linked_at = now
        pair.revoked_at = None
    code.consumed_at = now
    await REPO.clear_attempts(session, sender_digest)
    if commit:
        await session.commit()
    return True


async def resolve_sender(session: AsyncSession, sender_user_id: int, chat_id: int) -> int | None:
    """#98 calls this before intake. Recheck the active row under tenant RLS."""
    owner = await REPO.active_owner(session, sender_user_id, chat_id)
    if owner is None:
        return None
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(owner)}
    )
    link = await session.scalar(
        select(TelegramLink)
        .where(
            TelegramLink.usuario_id == owner,
            TelegramLink.telegram_user_id == sender_user_id,
            TelegramLink.telegram_chat_id == chat_id,
            TelegramLink.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if link is None:
        return None
    link.last_inbound_at = _now()
    await session.flush()
    return owner
