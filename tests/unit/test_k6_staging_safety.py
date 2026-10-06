"""The staging seeder must refuse a workstation or a path containing repository data."""

import importlib.util
import io
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/k6_staging.py"
SPEC = importlib.util.spec_from_file_location("k6_staging", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
staging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging)


def test_refuses_non_ci_machine(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("K6_STAGING_DIR", str(tmp_path / "runtime"))
    with pytest.raises(RuntimeError, match="isolated GitHub Actions"):
        staging.runtime_dir()


@pytest.mark.parametrize("destination", ["same", "outside", "traversal"])
def test_refuses_unowned_destination(monkeypatch, tmp_path, destination):
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    runner = tmp_path / "runner"
    paths = {
        "same": runner,
        "outside": tmp_path / "elsewhere",
        "traversal": runner / ".." / "escape",
    }
    monkeypatch.setenv("RUNNER_TEMP", str(runner))
    monkeypatch.setenv("K6_STAGING_DIR", str(paths[destination]))
    with pytest.raises(RuntimeError, match="child of RUNNER_TEMP"):
        staging.runtime_dir()


def test_refuses_credentials_inside_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setattr(staging, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("K6_STAGING_DIR", str(tmp_path / "runtime"))
    with pytest.raises(RuntimeError, match="outside the checkout"):
        staging.runtime_dir()


def test_accepts_only_isolated_runner_child(monkeypatch, tmp_path):
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("K6_STAGING_DIR", str(tmp_path / "runtime"))
    assert staging.runtime_dir() == tmp_path / "runtime"


def test_disposable_tls_accepts_verified_localhost_and_refuses_untrusted_certificate(tmp_path):
    staging.prepare_tls(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "tls.crt", tmp_path / "tls.key")

    class Endpoint(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"https://127.0.0.1:{server.server_port}/ready"
        trust = ssl.create_default_context(cafile=str(tmp_path / "tls.crt"))
        assert httpx.get(url, verify=trust).status_code == 200
        with pytest.raises(httpx.ConnectError):
            httpx.get(url, verify=ssl.create_default_context())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_seeded_extraction_matches_real_upload_clock_and_preflight_archive(monkeypatch):
    from bancaemdia.cache import extracao_cache
    from bancaemdia.domain.upload import ExportTelegram
    from bancaemdia.extracao.rodada import ler_mensagem
    from bancaemdia.workers.materialization import FUSO_DO_BRASIL

    class Cache:
        def __init__(self):
            self.entries = {}

        def guardar(self, key, reading, **_kwargs):
            self.entries[key] = SimpleNamespace(cupons=reading.bilhetes, modelo=reading.modelo)

        def buscar(self, key):
            return self.entries.get(key)

    class NoProvider:
        modelo_escalonamento = "synthetic-top"

        def ler(self, *_args, **_kwargs):
            pytest.fail("Synthetic warmed-cache upload attempted a provider call")

    cache = Cache()
    monkeypatch.setattr(extracao_cache, "get_cache", lambda: cache)
    staging.seed_extraction_cache({"ANTHROPIC_ESCALATION_MODEL": "synthetic-top"})
    archives = [
        (staging.ROOT / "k6/fixtures/telegram-small.zip").read_bytes(),
        staging.upload_preflight_archive(),
    ]
    message_ids = []
    for archive in archives:
        with ExportTelegram(io.BytesIO(archive), "synthetic.zip") as export:
            message = next(export.mensagens())
            image = export.bytes_da_foto(message)
            posted = message.data
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=FUSO_DO_BRASIL)
            # Round-trip through timestamptz as the actual upload worker does.
            posted = posted.astimezone(FUSO_DO_BRASIL).replace(tzinfo=None)
            assert cache.buscar(extracao_cache.chave_de_imagem(image, message.texto)) is None
            reading = ler_mensagem(
                NoProvider(), image, legenda=message.texto, postada_em=posted, cache=cache
            )
            assert reading.quantidade_de_apostas == 1 and reading.custo_usd == 0
            message_ids.append(message.message_id)
    assert message_ids[0] != message_ids[1] == 900001
