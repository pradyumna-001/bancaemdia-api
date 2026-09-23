"""Provider-neutral access decision. All instants are aware UTC timestamps."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

TRIAL_LENGTH = timedelta(days=7)


class SubscriptionStatus(StrEnum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class AccessMode(StrEnum):
    FULL_WRITE = "FULL_WRITE"
    READ_ONLY = "READ_ONLY"


@dataclass(frozen=True)
class BillingSnapshot:
    status: SubscriptionStatus
    trial_started_at: datetime
    trial_ends_at: datetime
    current_period_started_at: datetime | None = None
    current_period_ends_at: datetime | None = None
    price_id: int | None = None


@dataclass(frozen=True)
class BillingReadModel:
    status: SubscriptionStatus | None
    access: AccessMode
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    current_period_ends_at: datetime | None
    price_id: int | None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("billing timestamps must include a timezone")
    return value.astimezone(UTC)


def trial_bounds(started_at: datetime) -> tuple[datetime, datetime]:
    start = _aware(started_at)
    return start, start + TRIAL_LENGTH


def access_mode(
    snapshot: BillingSnapshot | None, *, now: datetime, rollout_at: datetime | None
) -> AccessMode:
    instant = _aware(now)
    if rollout_at is None:
        return AccessMode.FULL_WRITE
    _aware(rollout_at)
    if snapshot is None:
        # A failed backfill must never grant another seven days.
        return AccessMode.READ_ONLY
    start, end = _aware(snapshot.trial_started_at), _aware(snapshot.trial_ends_at)
    if end - start != TRIAL_LENGTH:
        raise ValueError("invalid trial bounds")
    if start <= instant < end:
        return AccessMode.FULL_WRITE
    if snapshot.status == SubscriptionStatus.ACTIVE:
        if (
            snapshot.current_period_started_at is not None
            and snapshot.current_period_ends_at is not None
            and _aware(snapshot.current_period_started_at)
            <= instant
            < _aware(snapshot.current_period_ends_at)
        ):
            return AccessMode.FULL_WRITE
    return AccessMode.READ_ONLY


def read_model(
    snapshot: BillingSnapshot | None, *, now: datetime, rollout_at: datetime | None
) -> BillingReadModel:
    mode = access_mode(snapshot, now=now, rollout_at=rollout_at)
    if snapshot is None:
        return BillingReadModel(None, mode, None, None, None, None)
    status = snapshot.status
    if status == SubscriptionStatus.TRIALING and mode == AccessMode.READ_ONLY:
        status = SubscriptionStatus.EXPIRED
    return BillingReadModel(
        status,
        mode,
        snapshot.trial_started_at,
        snapshot.trial_ends_at,
        snapshot.current_period_ends_at,
        snapshot.price_id,
    )
