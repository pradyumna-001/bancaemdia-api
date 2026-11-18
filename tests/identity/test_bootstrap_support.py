"""Sandbox bootstrap helpers: diagnostics never leak secrets, reserved ports stay reserved."""

import socket

from tests.identity import diagnostics, ports


def test_sanitize_removes_credentials_tokens_and_personal_data() -> None:
    raw = "\n".join([
        "asyncpg: password authentication failed postgresql+asyncpg://role:s3cr3t-pass@db/x",
        "GET http://127.0.0.1:58080/realms/r/login-actions/action-token?key=abc&client_id=c",
        "user person.name+tag@example.org; jwt eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        "Cookie: bancaemdia_session=opaque; KEYCLOAK_IDENTITY=other",
        "Authorization: Bearer abc.def",
        "secret=short1 code " + "A" * 40,
        "configured value hunter2-value appears",
    ])
    clean = diagnostics.sanitize(raw, {"KC_BOOTSTRAP_ADMIN_PASSWORD": "hunter2-value"})
    for leaked in (
        "s3cr3t-pass",
        "key=abc",
        "person.name",
        "eyJ",
        "bancaemdia_session",
        "KEYCLOAK_IDENTITY",
        "abc.def",
        "short1",
        "A" * 40,
        "hunter2-value",
    ):
        assert leaked not in clean
    # What a reader needs to tell infrastructure from product failures survives.
    assert "password authentication failed" in clean
    assert "/realms/r/login-actions/action-token?<query>" in clean


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_reserved_port_outlives_closed_duplicates() -> None:
    port = _free_port()
    first = ports.listener(port)
    first.close()  # A server_close() of one test must not release the port.
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
    second = ports.listener(port)
    assert second.getsockname() == ("127.0.0.1", port)
    second.close()
    third = ports.listener(port)
    assert third.fileno() != ports._LISTENERS[port].fileno()
    third.close()
