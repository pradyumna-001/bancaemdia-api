"""Resolve an account from the bet occurrence, independently of ingestion time."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ResolutionStatus(StrEnum):
    NONE = "NONE"
    UNIQUE = "UNIQUE"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class AccountUsage:
    conta_casa_id: int
    vigente_de: datetime | None
    vigente_ate: datetime | None


@dataclass(frozen=True)
class AccountResolution:
    status: ResolutionStatus
    conta_casa_id: int | None = None


def resolve_account(instant: datetime | None, usages: Iterable[AccountUsage]) -> AccountResolution:
    if instant is None:
        return AccountResolution(ResolutionStatus.NONE)
    matching = {
        usage.conta_casa_id
        for usage in usages
        if (usage.vigente_de is None or usage.vigente_de <= instant)
        and (usage.vigente_ate is None or instant < usage.vigente_ate)
    }
    if not matching:
        return AccountResolution(ResolutionStatus.NONE)
    if len(matching) > 1:
        return AccountResolution(ResolutionStatus.AMBIGUOUS)
    return AccountResolution(ResolutionStatus.UNIQUE, matching.pop())


def review_reason(result: AccountResolution) -> str | None:
    if result.status == ResolutionStatus.NONE:
        return "conta da aposta não identificada no instante em que foi feita"
    if result.status == ResolutionStatus.AMBIGUOUS:
        return "mais de uma conta possível no instante em que a aposta foi feita"
    return None
