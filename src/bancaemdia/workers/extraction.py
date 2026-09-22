import base64
import threading
from datetime import datetime

import anthropic
import pybreaker
from celery import chain, signals
from celery.canvas import Signature

from bancaemdia.cache.extracao_cache import get_cache
from bancaemdia.extracao.cliente import (
    VERSAO_PROMPT,
    Leitura,
    LeituraFalhouError,
    TipoDeImagem,
    get_leitor,
    tipo_da_imagem,
)
from bancaemdia.extracao.escada import Leitor
from bancaemdia.extracao.rodada import ler_mensagem
from bancaemdia.observability.metrics import anthropic_cost, batch_bets_processed, observe_stage
from bancaemdia.observability.tracing import custom_span
from bancaemdia.rate_limit.anthropic_limiter import AnthropicLimiter, get_limiter
from bancaemdia.workers.celery_app import EXTRACTION_QUEUE, app

RETRY_ON = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    pybreaker.CircuitBreakerError,
)


class LeitorLimitado:
    def __init__(
        self,
        leitor: Leitor,
        limiter: AnthropicLimiter,
        usuario_id: int,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> None:
        self.leitor = leitor
        self.limiter = limiter
        self.usuario_id = usuario_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.modelo_escalonamento = leitor.modelo_escalonamento

    def ler(
        self,
        imagem: bytes,
        tipo: TipoDeImagem = "image/jpeg",
        *,
        legenda: str = "",
        postada_em: datetime | None = None,
        escalonar: bool = False,
    ) -> Leitura:
        self.limiter.acquire(self.usuario_id)
        custo = anthropic_cost.labels(usuario_id=str(self.usuario_id))
        modelo = (
            self.modelo_escalonamento
            if escalonar
            else str(getattr(self.leitor, "modelo", "unknown"))
        )
        try:
            with custom_span(
                "extraction.chamar_anthropic",
                model=modelo,
                versao_prompt=VERSAO_PROMPT,
                chat_id=self.chat_id,
                message_id=self.message_id,
            ):
                leitura = self.leitor.ler(
                    imagem, tipo, legenda=legenda, postada_em=postada_em, escalonar=escalonar
                )
        except LeituraFalhouError as erro:
            # Resposta cortada, recusa ou JSON incompleto também foram cobrados.
            custo.inc(erro.custo_usd)
            raise
        custo.inc(leitura.custo_usd)
        return leitura


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
    with observe_stage(EXTRACTION_QUEUE):
        leitura = ler_mensagem(
            LeitorLimitado(
                get_leitor(),
                get_limiter(),
                usuario_id,
                chat_id=chat_id,
                message_id=message_id,
            ),
            base64.b64decode(imagem_base64),
            tipo_da_imagem(nome_do_arquivo),
            legenda=legenda,
            postada_em=datetime.fromisoformat(postada_em) if postada_em else None,
            casas_do_link=casas_do_link or (),
            odds_do_texto=odds_do_texto or (),
            cache=get_cache(),
        )
        batch_bets_processed.labels(stage=EXTRACTION_QUEUE).inc(leitura.quantidade_de_apostas)
    return {
        "usuario_id": usuario_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "postada_em": postada_em,
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


def limpar_cache_de_versoes_antigas(**kwargs: object) -> threading.Thread:
    tarefa = threading.Thread(
        target=get_cache().limpar_versoes_antigas, name="limpar-cache-extracao", daemon=True
    )
    tarefa.start()
    return tarefa


signals.worker_ready.connect(limpar_cache_de_versoes_antigas)


def cadeia_do_bilhete(
    usuario_id: int,
    imagem_base64: str,
    *,
    nome_do_arquivo: str,
    legenda: str,
    postada_em: str | None,
    casas_do_link: list[str],
    odds_do_texto: list[float],
    chat_id: int,
    message_id: int,
    midia_hash: str,
    upload_id: int | None = None,
) -> Signature:
    # A leitura entra como primeiro argumento posicional da gravação: `materializar_aposta` tem o
    # usuário nessa posição e a corrente trocaria os dois sem erro nenhum (medido).
    leitura = app.signature(
        "extraction.extrair_bilhete",
        kwargs={
            "usuario_id": usuario_id,
            "imagem_base64": imagem_base64,
            "nome_do_arquivo": nome_do_arquivo,
            "legenda": legenda,
            "postada_em": postada_em,
            "casas_do_link": casas_do_link,
            "odds_do_texto": odds_do_texto,
            "chat_id": chat_id,
            "message_id": message_id,
        },
    )
    gravacao = app.signature(
        "materialization.materializar_leitura",
        kwargs={"usuario_id": usuario_id, "midia_hash": midia_hash, "upload_id": upload_id},
    )
    corrente = chain(leitura, gravacao)
    if upload_id is None:
        return corrente
    # Sem este retorno de erro o envio ficaria "processando" para sempre quando a leitura esgota
    # as tentativas: é ele que fecha o bilhete como FALHOU.
    return corrente.on_error(
        app.signature(
            "materialization.registrar_falha",
            args=(upload_id, usuario_id, chat_id, message_id),
            immutable=True,
        )
    )
