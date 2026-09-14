import base64
from datetime import datetime

import anthropic
import pybreaker

from bancaemdia.extracao.cliente import VERSAO_PROMPT, get_leitor, tipo_da_imagem
from bancaemdia.extracao.rodada import ler_mensagem
from bancaemdia.workers.celery_app import app

RETRY_ON = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    pybreaker.CircuitBreakerError,
)


def extrair_bilhete(
    usuario_id: int,
    imagem_base64: str,
    nome_do_arquivo: str = "",
    legenda: str = "",
    postada_em: str | None = None,
    casas_do_link: list[str] | None = None,
    odds_do_texto: list[float] | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, object]:
    leitura = ler_mensagem(
        get_leitor(),
        base64.b64decode(imagem_base64),
        tipo_da_imagem(nome_do_arquivo),
        legenda=legenda,
        postada_em=datetime.fromisoformat(postada_em) if postada_em else None,
        casas_do_link=casas_do_link or (),
        odds_do_texto=odds_do_texto or (),
    )
    return {
        "usuario_id": usuario_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "versao_prompt": VERSAO_PROMPT,
        **leitura.para_json(),
    }


extrair_bilhete_task = app.task(
    name="extraction.extrair_bilhete",
    autoretry_for=RETRY_ON,
    retry_backoff=60,
    retry_backoff_max=600,
    retry_jitter=False,
    max_retries=3,
)(extrair_bilhete)
