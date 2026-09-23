from types import SimpleNamespace

import pytest

from bancaemdia.domain import account_review
from bancaemdia.domain.account_attribution import AccountResolution, ResolutionStatus


@pytest.mark.asyncio
async def test_unassigned_review_is_idempotent_and_resolves_after_attribution(monkeypatch) -> None:
    rows = []

    class Repo:
        async def list_abertas_by_aposta_chave(self, session, user_id, key):
            return [r for r in rows if not r.resolved]

        async def has_open(self, session, user_id, key, reason):
            return any(r.motivo == reason and not r.resolved for r in rows)

        async def create(self, session, data):
            rows.append(
                SimpleNamespace(
                    id=len(rows) + 1,
                    motivo=data["motivo"],
                    extracao_bruta=data["extracao_bruta"],
                    resolved=False,
                )
            )

        async def resolve(self, session, user_id, id_):
            rows[id_ - 1].resolved = True

    monkeypatch.setattr(account_review, "RevisaoPendenteRepo", Repo)
    missing = AccountResolution(ResolutionStatus.NONE)
    assert await account_review.sync_account_review(None, 7, "m:1", missing)
    assert await account_review.sync_account_review(None, 7, "m:1", missing) is None
    assert len(rows) == 1 and rows[0].extracao_bruta["tipo_revisao"] == "conta"

    assigned = AccountResolution(ResolutionStatus.UNIQUE, 10)
    assert await account_review.sync_account_review(None, 7, "m:1", assigned) is None
    assert rows[0].resolved
