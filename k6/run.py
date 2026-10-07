"""Run k6 with stable, unique per-scenario credential leases, held only on loopback."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tokens(name: str) -> list[str]:
    result = []
    for part in [name, *(f"{name}_{index}" for index in range(1, 6))]:
        if os.environ.get(part):
            values = json.loads(os.environ[part])
            if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
                raise ValueError(f"{part} must be an array of nonempty tokens")
            result.extend(values)
    return result


class CredentialLeases:
    def __init__(self, jwt: list[str], coleta: list[str], profile: str) -> None:
        offset = 50 if profile == "all" else 0
        self.pools = {
            "steady": jwt[:50],
            "spike": jwt[offset : offset + 200],
            "painel": jwt[:100],
            "cd-smoke": jwt[:1],
            "coleta": coleta[:100],
        }
        self.leases: dict[str, dict[int, str]] = {name: {} for name in self.pools}
        self.lock = threading.Lock()

    def acquire(self, scenario: str, vu: int) -> str:
        if vu < 1 or scenario not in self.pools:
            raise ValueError("Invalid scenario or VU")
        with self.lock:
            leases = self.leases[scenario]
            if vu not in leases:
                if len(leases) >= len(self.pools[scenario]):
                    raise ValueError("Insufficient distinct credentials for the scenario")
                leases[vu] = self.pools[scenario][len(leases)]
            return leases[vu]


def handler_for(leases: CredentialLeases) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                _, scenario, vu = self.path.split("/")
                payload = {"token": leases.acquire(scenario, int(vu))}
                status = 200
            except (ValueError, KeyError):
                payload = {"error": "Credential allocation refused"}
                status = 400
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # Credentials, VU IDs and request bodies must never reach access logs.
            pass

    return Handler


class CredentialServer(ThreadingHTTPServer):
    # Constant-VU scenarios start 100 users together. The stdlib default backlog of five
    # dropped/timed out leases before they could reach the handler on the Linux runner.
    request_queue_size = 512


def main() -> int:
    leases = CredentialLeases(
        tokens("JWT_TOKENS_JSON"),
        tokens("COLETA_TOKENS_JSON"),
        os.environ.get("LOAD_PROFILE", "all"),
    )
    server = CredentialServer(("127.0.0.1", 0), handler_for(leases))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        env = {**os.environ, "K6_CREDENTIALS_URL": f"http://127.0.0.1:{server.server_port}"}
        return subprocess.run(["k6", "run", "k6/load-test.js"], env=env, check=False).returncode
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    raise SystemExit(main())
