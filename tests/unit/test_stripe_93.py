"""Deterministic Stripe HTTP contracts. These do not claim account/sandbox approval."""

import pytest


def test_expired_worker_never_calls_extractor(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from bancaemdia.domain.access import AccountReadOnlyError
    from bancaemdia.workers import extraction

    monkeypatch.setattr(
        "bancaemdia.domain.access.require_worker_write_access",
        AsyncMock(side_effect=AccountReadOnlyError("account_read_only")),
    )
    reader = Mock()
    monkeypatch.setattr(extraction, "ler_mensagem", reader)
    with pytest.raises(AccountReadOnlyError):
        extraction.extrair_bilhete(123, "fixture")
    reader.assert_not_called()
