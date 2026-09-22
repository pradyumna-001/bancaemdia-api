from __future__ import annotations

import inspect
import tomllib
from importlib.metadata import version
from pathlib import Path

from slowapi import Limiter

SLOWAPI_VERSION = "0.1.10"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_slowapi_is_exactly_pinned_to_the_exercised_version() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declarations = [
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.lower().startswith("slowapi")
    ]

    assert declarations == [f"slowapi=={SLOWAPI_VERSION}"], (
        "SlowAPI must remain exactly pinned because the rate-limit middleware uses private APIs"
    )
    assert version("slowapi") == SLOWAPI_VERSION, (
        "The installed SlowAPI version differs from the private API contract exercised here"
    )


def test_slowapi_private_middleware_contract_is_unchanged() -> None:
    assert tuple(inspect.signature(Limiter._check_request_limit).parameters) == (
        "self",
        "request",
        "endpoint_func",
        "in_middleware",
    ), "SlowAPI changed _check_request_limit; review RateLimitMiddleware before upgrading"
    assert tuple(inspect.signature(Limiter._inject_headers).parameters) == (
        "self",
        "response",
        "current_limit",
    ), "SlowAPI changed _inject_headers; review rate-limit response headers before upgrading"

    limiter = Limiter(
        key_func=lambda: "contract-key",
        default_limits=["1/minute"],
        in_memory_fallback=["1/minute"],
        in_memory_fallback_enabled=True,
    )
    assert hasattr(limiter, "_storage_dead") and isinstance(limiter._storage_dead, bool), (
        "SlowAPI removed or changed _storage_dead; fallback transition telemetry must be adapted"
    )
    assert hasattr(limiter, "_fallback_storage"), (
        "SlowAPI removed _fallback_storage; reset_rate_limiters must be adapted"
    )
    assert callable(getattr(limiter._fallback_storage, "reset", None)), (
        "SlowAPI fallback storage no longer supports reset; isolated tests must be adapted"
    )
