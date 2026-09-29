"""Legacy unit fakes model the pre-rollout database; billing has separate real-DB tests."""

from unittest.mock import AsyncMock

import pytest

from bancaemdia.domain.billing import AccessMode, BillingReadModel


@pytest.fixture(autouse=True)
def legacy_billing_rollout_inactive(request, monkeypatch):
    if request.node.path.name in {
        "test_apostas_api.py",
        "test_caixa_api.py",
        "test_coleta_api.py",
        "test_materialization.py",
        "test_reprocess.py",
        "test_upload_api.py",
        "test_upload_worker.py",
        "test_extraction.py",
    }:
        monkeypatch.setattr(
            "bancaemdia.repositories.assinatura_repo.AssinaturaRepo.read_status",
            AsyncMock(
                return_value=BillingReadModel(None, AccessMode.FULL_WRITE, None, None, None, None)
            ),
        )

    if request.node.path.name == "test_extraction.py":
        monkeypatch.setattr("bancaemdia.domain.access.require_worker_write_access", AsyncMock())
