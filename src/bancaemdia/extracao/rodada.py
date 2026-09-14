from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import anthropic

from bancaemdia.domain.conferencias import CONFIANCA_MINIMA, Veredito
from bancaemdia.extracao.cliente import LeituraFalhouError, TipoDeImagem
from bancaemdia.extracao.escada import Degrau, EscalonamentoFalhouError, Leitor, extrair
from bancaemdia.extracao.modelos import ExtracaoBilhete

ERROS_DO_BILHETE = (
    anthropic.BadRequestError,
    anthropic.RequestTooLargeError,
    anthropic.UnprocessableEntityError,
)


@dataclass(frozen=True)
class CupomLido:
    bilhete: ExtracaoBilhete
    motivo: str | None = None
    grave: bool = False

    def para_json(self) -> dict[str, object]:
        return {
            "bilhete": self.bilhete.model_dump(mode="json"),
            "motivo": self.motivo,
            "grave": self.grave,
        }


@dataclass(frozen=True)
class LeituraDaMensagem:
    bilhete: ExtracaoBilhete | None = None
    motivo: str | None = None
    grave: bool = False
    cupons: tuple[CupomLido, ...] = ()
    degrau: Degrau | None = None
    custo_usd: float = 0.0
    nao_e_aposta: bool = False

    def para_json(self) -> dict[str, object]:
        return {
            "bilhete": None if self.bilhete is None else self.bilhete.model_dump(mode="json"),
            "motivo": self.motivo,
            "grave": self.grave,
            "cupons": [c.para_json() for c in self.cupons],
            "degrau": None if self.degrau is None else str(self.degrau),
            "custo_usd": self.custo_usd,
            "nao_e_aposta": self.nao_e_aposta,
        }


def leitura_que_falhou(erro: BaseException, custo_usd: float = 0.0) -> LeituraDaMensagem:
    return LeituraDaMensagem(
        motivo=f"a leitura falhou ({type(erro).__name__}) — preencha a odd olhando o print",
        grave=True,
        custo_usd=custo_usd,
    )


def ler_mensagem(
    leitor: Leitor,
    imagem: bytes,
    tipo: TipoDeImagem = "image/jpeg",
    *,
    legenda: str = "",
    postada_em: datetime | None = None,
    casas_do_link: Iterable[str] = (),
    odds_do_texto: Iterable[float] = (),
    confianca_minima: float = CONFIANCA_MINIMA,
) -> LeituraDaMensagem:
    try:
        saida = extrair(
            leitor,
            imagem,
            tipo,
            legenda=legenda,
            postada_em=postada_em,
            casas_do_link=casas_do_link,
            odds_do_texto=odds_do_texto,
            confianca_minima=confianca_minima,
        )
    except LeituraFalhouError as erro:
        return leitura_que_falhou(erro, erro.custo_usd)
    except EscalonamentoFalhouError as erro:
        return leitura_que_falhou(erro.__cause__ or erro, erro.custo_usd)
    except ERROS_DO_BILHETE as erro:
        return leitura_que_falhou(erro)

    if saida.parecer.veredito is Veredito.NAO_E_APOSTA:
        return LeituraDaMensagem(degrau=saida.degrau, custo_usd=saida.custo_usd, nao_e_aposta=True)

    cupons = tuple(CupomLido(b, p.motivo, p.grave) for b, p in saida.pares)
    return LeituraDaMensagem(
        bilhete=saida.bilhete,
        motivo=saida.parecer.motivo,
        grave=saida.parecer.grave,
        cupons=cupons if len(cupons) > 1 else (),
        degrau=saida.degrau,
        custo_usd=saida.custo_usd,
    )
