import base64
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

import anthropic
import pybreaker
from anthropic.types import ContentBlockParam, TextBlock, Usage
from pydantic import ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from bancaemdia.config import get_settings
from bancaemdia.extracao.modelos import Cupons, ExtracaoBilhete
from bancaemdia.observability.metrics import observe_anthropic_request
from bancaemdia.resilience.circuit_breaker import get_anthropic_breaker, new_anthropic_breaker

TipoDeImagem = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

VERSAO_PROMPT = "extrair_bilhete_v3"
PASTA_PROMPTS = Path(__file__).parent / "prompts"
ESQUEMA_DOS_CUPONS = anthropic.transform_schema(Cupons)

PRECOS: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}
GRAVACAO_NO_CACHE = 1.25
LEITURA_DO_CACHE = 0.1

TIPOS_DE_IMAGEM: dict[str, TipoDeImagem] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_TOKENS = 16000
TENTATIVAS_DE_LEITURA = 3
LIMITE_DA_LEGENDA = 600


class LeituraFalhouError(Exception):
    def __init__(self, mensagem: str, custo_usd: float = 0.0) -> None:
        super().__init__(mensagem)
        self.custo_usd = custo_usd


@dataclass(frozen=True)
class Leitura:
    bilhetes: tuple[ExtracaoBilhete, ...]
    modelo: str
    tokens_entrada: int = 0
    tokens_saida: int = 0
    tokens_cache_lidos: int = 0
    tokens_cache_gravados: int = 0
    custo_usd: float = 0.0


def carregar_prompt() -> str:
    return (PASTA_PROMPTS / "extrair_bilhete.md").read_text(encoding="utf-8")


def tipo_da_imagem(nome_do_arquivo: str) -> TipoDeImagem:
    return TIPOS_DE_IMAGEM.get(Path(nome_do_arquivo).suffix.lower(), "image/jpeg")


def calcular_custo(
    modelo: str, entrada: int, saida: int, cache_lidos: int = 0, cache_gravados: int = 0
) -> float:
    preco_entrada, preco_saida = PRECOS[modelo]
    return (
        entrada * preco_entrada
        + cache_gravados * preco_entrada * GRAVACAO_NO_CACHE
        + cache_lidos * preco_entrada * LEITURA_DO_CACHE
        + saida * preco_saida
    ) / 1e6


def somar_usos(usos: Sequence[Usage]) -> tuple[int, int, int, int]:
    return (
        sum(u.input_tokens for u in usos),
        sum(u.output_tokens for u in usos),
        sum(u.cache_read_input_tokens or 0 for u in usos),
        sum(u.cache_creation_input_tokens or 0 for u in usos),
    )


def cupons_da_resposta(cupons: Cupons | None) -> tuple[ExtracaoBilhete, ...]:
    if cupons is None or not cupons.cupons:
        return (ExtracaoBilhete(ilegivel=True, confianca=0.0),)
    return tuple(cupons.cupons)


def montar_conteudo(
    imagem: bytes,
    tipo: TipoDeImagem,
    legenda: str = "",
    postada_em: datetime | None = None,
) -> list[ContentBlockParam]:
    conteudo: list[ContentBlockParam] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": tipo,
                "data": base64.standard_b64encode(imagem).decode("ascii"),
            },
        }
    ]
    if postada_em is not None:
        conteudo.append({
            "type": "text",
            "text": (
                f"Este print foi enviado em {postada_em:%d/%m/%Y às %H:%M}. Use esta data para "
                "resolver referências relativas no bilhete ('Hoje', 'Amanhã') e para completar "
                "o ano quando o bilhete mostrar só dia e mês."
            ),
        })
    if legenda.strip():
        conteudo.append({
            "type": "text",
            "text": (
                "Legenda que o apostador escreveu junto com este print. Use para desempatar "
                "QUAL aposta do print é a que vale, e para identificar a casa quando o logotipo "
                "não aparecer. A stake em unidades vem daqui e NÃO deve ser extraída da "
                "imagem.\n\n" + legenda.strip()[:LIMITE_DA_LEGENDA]
            ),
        })
    conteudo.append({
        "type": "text",
        "text": (
            "Leia TODOS os cupons de aposta desta imagem, na ordem em que aparecem "
            "(de cima para baixo)."
        ),
    })
    return conteudo


class LeitorDeBilhetes:
    def __init__(
        self,
        client: anthropic.Anthropic,
        modelo: str,
        modelo_escalonamento: str,
        breaker: pybreaker.CircuitBreaker | None = None,
        pausa: Callable[[float], None] = time.sleep,
    ) -> None:
        for nome in (modelo, modelo_escalonamento):
            if nome not in PRECOS:
                raise ValueError(f"modelo sem preço cadastrado: {nome}")
        self.client = client
        self.modelo = modelo
        self.modelo_escalonamento = modelo_escalonamento
        self.breaker = new_anthropic_breaker() if breaker is None else breaker
        self.pausa = pausa
        self.prompt = carregar_prompt()

    def ler(
        self,
        imagem: bytes,
        tipo: TipoDeImagem = "image/jpeg",
        *,
        legenda: str = "",
        postada_em: datetime | None = None,
        escalonar: bool = False,
    ) -> Leitura:
        modelo = self.modelo_escalonamento if escalonar else self.modelo
        conteudo = montar_conteudo(imagem, tipo, legenda, postada_em)
        usos: list[Usage] = []
        tentativas = Retrying(
            sleep=self.pausa,
            stop=stop_after_attempt(TENTATIVAS_DE_LEITURA),
            wait=wait_exponential_jitter(initial=1, max=4),
            retry=retry_if_exception_type(LeituraFalhouError),
            reraise=True,
        )
        try:
            cupons = tentativas(self._chamar, modelo, conteudo, usos)
        except LeituraFalhouError as erro:
            erro.custo_usd = calcular_custo(modelo, *somar_usos(usos))
            raise
        entrada, saida, lidos, gravados = somar_usos(usos)
        return Leitura(
            bilhetes=cupons_da_resposta(cupons),
            modelo=modelo,
            tokens_entrada=entrada,
            tokens_saida=saida,
            tokens_cache_lidos=lidos,
            tokens_cache_gravados=gravados,
            custo_usd=calcular_custo(modelo, entrada, saida, lidos, gravados),
        )

    def _chamar(
        self, modelo: str, conteudo: list[ContentBlockParam], usos: list[Usage]
    ) -> Cupons | None:
        with self.breaker.calling(), observe_anthropic_request(modelo):
            resposta = self.client.messages.create(
                model=modelo,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": self.prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": conteudo}],
                output_config={"format": {"type": "json_schema", "schema": ESQUEMA_DOS_CUPONS}},
            )
        usos.append(resposta.usage)
        if resposta.stop_reason == "max_tokens":
            raise LeituraFalhouError(
                f"resposta do {modelo} bateu no teto de {MAX_TOKENS} tokens e veio cortada"
                " — pode estar faltando perna da múltipla"
            )
        if resposta.stop_reason == "refusal":
            raise LeituraFalhouError(f"o {modelo} recusou ler esta imagem")
        texto = next((b.text for b in resposta.content if isinstance(b, TextBlock)), None)
        if texto is None:
            return None
        try:
            return Cupons.model_validate_json(texto)
        except ValidationError as erro:
            raise LeituraFalhouError(f"resposta do {modelo} veio incompleta: {erro}") from erro


@lru_cache
def get_leitor() -> LeitorDeBilhetes:
    settings = get_settings()
    client = anthropic.Anthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=settings.ANTHROPIC_TIMEOUT,
        max_retries=settings.ANTHROPIC_MAX_RETRIES,
    )
    return LeitorDeBilhetes(
        client,
        settings.ANTHROPIC_MODEL,
        settings.ANTHROPIC_ESCALATION_MODEL,
        breaker=get_anthropic_breaker(),
    )
