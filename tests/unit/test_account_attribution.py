from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bancaemdia.domain import account_attribution_service as service
from bancaemdia.domain.account_attribution import (
    AccountUsage,
    ResolutionStatus,
    resolve_account,
    review_reason,
)

START = datetime(2026, 9, 1, tzinfo=UTC)
SWITCH = START + timedelta(days=5)


def test_event_time_uses_half_open_intervals_even_after_a_later_capture() -> None:
    usages = [AccountUsage(10, START, SWITCH), AccountUsage(20, SWITCH, None)]

    assert resolve_account(SWITCH - timedelta(microseconds=1), usages).conta_casa_id == 10
    assert resolve_account(SWITCH, usages).conta_casa_id == 20
    assert resolve_account(START - timedelta(seconds=1), usages).status == ResolutionStatus.NONE
    assert resolve_account(None, usages).status == ResolutionStatus.NONE


def test_zero_and_multiple_accounts_fail_closed_with_review_reasons() -> None:
    none = resolve_account(START, [])
    ambiguous = resolve_account(START, [AccountUsage(10, None, None), AccountUsage(20, None, None)])

    assert none.conta_casa_id is None and "não identificada" in review_reason(none)
    assert ambiguous.status == ResolutionStatus.AMBIGUOUS
    assert ambiguous.conta_casa_id is None and "mais de uma" in review_reason(ambiguous)


@pytest.mark.asyncio
async def test_explicit_reference_is_validated_for_tenant_and_house(monkeypatch) -> None:
    class CasaRepo:
        async def get_id_by_nome(self, session, name):
            return {"Betano": 1, "Outra": 2}.get(name)

    class ContaCasaRepo:
        async def get_by_id(self, session, user_id, account_id):
            if (user_id, account_id) == (7, 10):
                return SimpleNamespace(casa_id=1)
            return None

    class UsoContaCasaRepo:
        async def list_by_house(self, session, user_id, house_id):
            return [SimpleNamespace(conta_casa_id=10, vigente_de=START, vigente_ate=None)]

    monkeypatch.setattr(service, "CasaRepo", CasaRepo)
    monkeypatch.setattr(service, "ContaCasaRepo", ContaCasaRepo)
    monkeypatch.setattr(service, "UsoContaCasaRepo", UsoContaCasaRepo)
    assert (await service.attribute_account(None, 7, "Betano", START, 10)).conta_casa_id == 10
    assert (await service.attribute_account(None, 7, "Betano", START)).conta_casa_id == 10
    with pytest.raises(service.InvalidAccountReferenceError):
        await service.attribute_account(None, 7, "Outra", START, 10)
    with pytest.raises(service.InvalidAccountReferenceError):
        await service.attribute_account(None, 8, "Betano", START, 10)
