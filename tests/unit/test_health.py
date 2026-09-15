from __future__ import annotations

from fastapi.testclient import TestClient

from bancaemdia import main


def _health(monkeypatch, db_ok, states):
    class Database:
        async def check(self):
            return db_ok

    monkeypatch.setattr(main, "check_db_health", Database().check)
    monkeypatch.setattr(main, "breaker_states", lambda: states)
    return TestClient(main.app).get("/health")


def test_health_includes_the_circuit_breakers(monkeypatch) -> None:
    response = _health(monkeypatch, True, {"anthropic": "closed"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": "ok",
        "circuit_breakers": {"anthropic": "closed"},
    }


def test_open_breaker_is_reported_without_failing_the_health_check(monkeypatch) -> None:
    response = _health(monkeypatch, True, {"anthropic": "open"})

    assert response.status_code == 200
    assert response.json()["circuit_breakers"] == {"anthropic": "open"}


def test_breakers_are_reported_when_the_database_is_down(monkeypatch) -> None:
    response = _health(monkeypatch, False, {"anthropic": "half-open"})

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "db": "unreachable",
        "circuit_breakers": {"anthropic": "half-open"},
    }
