"""Minimal local endpoint contract for k6/smoke.js; never used for load results."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def respond(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self.respond(
                200,
                b'celery_queue_depth{queue="extraction"} 2\n'
                b'pg_replication_lag_seconds{role="replica"} 0.25\n',
                "text/plain",
            )
        elif self.path == "/api/v1/painel":
            self.respond(200, b'{"resumo":{}}')
        elif self.path.startswith("/api/v1/upload/"):
            self.respond(200, b'{"status":"completed"}')
        else:
            self.respond(404, b"{}")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/api/v1/upload":
            self.respond(
                202,
                json.dumps({
                    "job_id": "00000000-0000-0000-0000-000000000001",
                    "status_url": "/api/v1/upload/00000000-0000-0000-0000-000000000001",
                }).encode(),
            )
        elif self.path == "/coleta":
            self.respond(200, b"{}")
        else:
            self.respond(404, b"{}")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
