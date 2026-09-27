"""Real middleware and HTTP serialization, with JWT verification isolated."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from bancaemdia.auth import middleware as auth
from bancaemdia.main import app
from bancaemdia.middleware.rate_limit import api_limiter, policy_for
from bancaemdia.middleware.router import get_recent_writes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def verify(_token: str, _cache: object) -> int:
        return await asyncio.sleep(0, result=103103)

    monkeypatch.setattr(auth, "verify_token", verify)
    monkeypatch.setattr(auth, "get_jwks_cache", lambda: None)
    monkeypatch.setattr(api_limiter, "enabled", False)
    yield TestClient(app)


HEADERS = {"Authorization": "Bearer calculator-contract-token"}
PREFIX = "/api/v1/calculadoras/"


@pytest.mark.contract
def test_all_nine_routes_authenticate_and_serialize_exact_decimals(client: TestClient) -> None:
    cases = {
        "probabilidade-implicita": {"odd": "2.00"},
        "mercado-justo": {"outcomes": [{"name": "A", "odd": "2"}, {"name": "B", "odd": "2"}]},
        "rtp": {"outcomes": [{"name": "A", "odd": "2"}, {"name": "B", "odd": "2"}]},
        "surebet": {
            "outcomes": [{"name": "A", "odd": "2.1"}, {"name": "B", "odd": "2.1"}],
            "total_stake_centavos": 100,
        },
        "dutching": {
            "outcomes": [{"name": "A", "odd": "1.9"}, {"name": "B", "odd": "1.9"}],
            "total_stake_centavos": 100,
        },
        "dividir-stake": {
            "mode": "percentages",
            "total_stake_centavos": 101,
            "selections": [{"name": "A", "value": "50"}, {"name": "B", "value": "50"}],
        },
        "cobertura-ao-vivo": {
            "original_stake_centavos": 10000,
            "original_odd": "1.5",
            "opposing_odd": "3",
            "objective": "protect_stake",
        },
        "lucro-alvo": {"odd": "3", "target_profit_centavos": 101},
        "percentual-banca": {"bankroll_centavos": 10000, "percentage": "1.25"},
    }
    for path, body in cases.items():
        assert client.post(PREFIX + path, json=body).status_code == 401
        response = client.post(PREFIX + path, json=body, headers=HEADERS)
        assert response.status_code == 200, (path, response.text)
        payload = response.json()
        assert set(payload) == {
            "data",
            "method",
            "precision",
            "rounding",
            "assumptions",
            "warnings",
        }
        assert isinstance(payload["data"], dict)
    implied = client.post(PREFIX + "probabilidade-implicita", json={"odd": "2.00"}, headers=HEADERS)
    assert implied.json()["data"] == {"probability": "0.50000000", "percentage": "50.000000"}
    scaled = client.post(
        PREFIX + "mercado-justo",
        json={"outcomes": [{"name": "A", "odd": "2.0"}, {"name": "B", "odd": "2.00"}]},
        headers=HEADERS,
    )
    assert [leg["odd"] for leg in scaled.json()["data"]["outcomes"]] == ["2", "2"]
    assert not get_recent_writes().recent(103103)


@pytest.mark.contract
def test_invalid_inputs_and_structural_limits(client: TestClient) -> None:
    for bad in ["NaN", "Infinity", "1e999999", "1", "2.000000000"]:
        response = client.post(
            PREFIX + "probabilidade-implicita", json={"odd": bad}, headers=HEADERS
        )
        assert response.status_code == 422
        assert bad not in response.text
    assert (
        client.post(
            PREFIX + "rtp", json={"outcomes": [{"name": "A", "odd": "2"}]}, headers=HEADERS
        ).status_code
        == 422
    )
    assert (
        client.post(
            PREFIX + "cobertura-ao-vivo",
            json={
                "original_stake_centavos": 100,
                "original_odd": "2",
                "opposing_odd": "2",
                "objective": "protect_stake",
                "stake_type": "freebet",
            },
            headers=HEADERS,
        ).status_code
        == 422
    )
    assert (
        client.post(
            PREFIX + "percentual-banca",
            json={"bankroll_centavos": 100, "percentage": "1", "stake_centavos": 1},
            headers=HEADERS,
        ).status_code
        == 422
    )


@pytest.mark.contract
def test_api_rate_policy_applies_and_post_is_not_a_financial_write(client: TestClient) -> None:
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": PREFIX + "rtp", "headers": []})
    assert policy_for(request).limiter is api_limiter
    response = client.post(
        PREFIX + "lucro-alvo", json={"odd": "2", "target_profit_centavos": 100}, headers=HEADERS
    )
    assert response.status_code == 200
    assert not get_recent_writes().recent(103103)
