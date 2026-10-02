"""Global VU IDs can be sparse/reused; leases must still give distinct scenario users."""

import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

import pytest

SPEC = importlib.util.spec_from_file_location(
    "k6_run", Path(__file__).resolve().parents[2] / "k6/run.py"
)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def test_concurrent_sparse_vus_have_unique_stable_credentials():
    jwt = [f"synthetic-user-{i}" for i in range(250)]
    leases = launcher.CredentialLeases(jwt, [], "all")
    ids = [10 + 7 * i for i in range(200)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        allocated = list(pool.map(lambda vu: leases.acquire("spike", vu), ids))
    assert len(set(allocated)) == 200
    assert set(allocated) == set(jwt[50:])
    assert all(
        leases.acquire("spike", vu) == token for vu, token in zip(ids, allocated, strict=True)
    )
    assert leases.acquire("steady", ids[0]) == jwt[0]
    with pytest.raises(ValueError, match="Insufficient"):
        leases.acquire("spike", 1)


@pytest.mark.parametrize("scenario,vu", [("unknown", 1), ("steady", 0), ("steady", -1)])
def test_invalid_credential_requests_are_refused(scenario, vu):
    leases = launcher.CredentialLeases(["synthetic-user"], [], "cd-smoke")
    with pytest.raises(ValueError, match="Invalid"):
        leases.acquire(scenario, vu)


def test_http_server_accepts_one_hundred_simultaneous_distinct_leases():
    tokens = [f"synthetic-collection-{i}" for i in range(100)]
    leases = launcher.CredentialLeases([], tokens, "coleta")
    server = launcher.CredentialServer(("127.0.0.1", 0), launcher.handler_for(leases))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    gate = threading.Barrier(101)

    def acquire(vu):
        gate.wait(timeout=10)
        with urlopen(f"http://127.0.0.1:{server.server_port}/coleta/{vu}", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            return json.load(response)["token"]

    try:
        with ThreadPoolExecutor(max_workers=100) as pool:
            futures = [pool.submit(acquire, 10 + 7 * i) for i in range(100)]
            gate.wait(timeout=10)
            allocated = [future.result() for future in futures]
        assert set(allocated) == set(tokens)
        assert len(set(allocated)) == 100
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
