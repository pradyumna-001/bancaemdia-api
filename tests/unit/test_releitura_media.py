from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import structlog
from structlog.testing import capture_logs

from bancaemdia.cli import reprocess
from bancaemdia.extracao.cliente import VERSAO_PROMPT


class _Resultado:
    def __init__(self, valor):
        self.valor = valor

    def scalar_one_or_none(self):
        return self.valor


class _Session:
    def __init__(self, foto: bytes | None):
        self.foto = foto

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def scalar(self, stmt):
        return 1

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM mensagens" in sql:
            return _Resultado(
                SimpleNamespace(texto="Betano odd 2.0", data=datetime(2026, 9, 20, 12, tzinfo=UTC))
            )
        if "FROM midias" in sql:
            return _Resultado(SimpleNamespace(tipo="image/png"))
        return _Resultado(None)


async def _identidade(session, usuario):  # ruff: ignore[unused-async]
    return None


def _instalar(monkeypatch, foto: bytes | None, publicados: list[dict]) -> None:
    class UsuarioRepo:
        async def list_active_ids(self, session, depois_de, limite):
            return [7] if depois_de == 0 else []

    class MidiaRepo:
        async def get_by_hash(self, session, hash_):
            return foto

    async def plano(engine, usuario_id, desde, ate, escopo, resultado):  # ruff: ignore[unused-async]
        resultado.usuarios += 1

    async def pagina(  # ruff: ignore[unused-async]
        session, usuario_id, ultimo, depois, escopo, tamanho, desde, ate
    ):
        if depois is not None:
            return []
        return [SimpleNamespace(chat_id=100, message_id=200, midia_hash="abc")]

    def cadeia(usuario_id, imagem_base64, **kwargs):
        class Corrente:
            def apply_async(self):
                structlog.get_logger().info("publicado")
                publicados.append({"usuario_id": usuario_id, "imagem": imagem_base64, **kwargs})

        return Corrente()

    monkeypatch.setattr(reprocess, "AsyncSession", lambda engine: _Session(foto))
    monkeypatch.setattr(reprocess, "UsuarioRepo", UsuarioRepo)
    monkeypatch.setattr(reprocess, "MidiaArquivoRepo", MidiaRepo)
    monkeypatch.setattr(reprocess, "_planejar", plano)
    monkeypatch.setattr(reprocess, "_pagina_midias", pagina)
    monkeypatch.setattr(reprocess, "_set_current_user", _identidade)
    monkeypatch.setattr(reprocess, "cadeia_do_bilhete", cadeia)


async def test_cost_precedes_publication_and_media_is_read_one_at_a_time(monkeypatch) -> None:
    publicados = []
    _instalar(monkeypatch, b"photo", publicados)

    with capture_logs() as logs:
        plano = await reprocess.reler_todas(object(), VERSAO_PROMPT, sim=True)

    assert (plano.bilhetes, plano.enfileiradas) == (1, 1)
    assert publicados[0]["nome_do_arquivo"] == "abc.png"
    assert publicados[0]["versao_prompt"] == VERSAO_PROMPT
    eventos = [entry["event"] for entry in logs]
    assert eventos.index("releitura_custo_antes_da_fila") < eventos.index("publicado")


async def test_missing_media_is_reported_without_queueing(monkeypatch) -> None:
    publicados = []
    _instalar(monkeypatch, None, publicados)

    with capture_logs() as logs:
        plano = await reprocess.reler_todas(object(), VERSAO_PROMPT, sim=True)

    assert plano.midias_ausentes == 1
    assert publicados == []
    assert any(entry["event"] == "releitura_midia_ausente" for entry in logs)


async def test_unknown_prompt_never_publishes(monkeypatch) -> None:
    publicados = []
    _instalar(monkeypatch, b"photo", publicados)
    with pytest.raises(reprocess.VersaoDoPromptError):
        await reprocess.reler_todas(object(), "extrair_bilhete_v999", sim=True)
    assert publicados == []
