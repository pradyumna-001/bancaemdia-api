from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from bancaemdia.cache.extracao_cache import ExtracaoCache, chave_de_imagem
from bancaemdia.domain.conferencias import CONFIANCA_MINIMA, Origem, Parecer, Veredito, conferir
from bancaemdia.extracao.cliente import Leitura, LeituraFalhouError, TipoDeImagem
from bancaemdia.extracao.modelos import ExtracaoBilhete


class Degrau(StrEnum):
    CACHE = "CACHE"
    BARATO = "BARATO"
    CARO = "CARO"


class EscalonamentoFalhouError(Exception):
    def __init__(self, custo_usd: float) -> None:
        super().__init__(f"a releitura falhou depois de gastar US$ {custo_usd:.6f}")
        self.custo_usd = custo_usd


class Leitor(Protocol):
    modelo_escalonamento: str

    def ler(
        self,
        imagem: bytes,
        tipo: TipoDeImagem = "image/jpeg",
        *,
        legenda: str = "",
        postada_em: datetime | None = None,
        escalonar: bool = False,
    ) -> Leitura: ...


@dataclass(frozen=True)
class Extraida:
    bilhetes: tuple[ExtracaoBilhete, ...]
    pareceres: tuple[Parecer, ...]
    degrau: Degrau
    custo_usd: float = 0.0

    @property
    def pares(self) -> list[tuple[ExtracaoBilhete, Parecer]]:
        return list(zip(self.bilhetes, self.pareceres, strict=True))

    @property
    def bilhete(self) -> ExtracaoBilhete:
        return self.bilhetes[0]

    @property
    def parecer(self) -> Parecer:
        return next(
            (p for p in self.pareceres if p.veredito is not Veredito.APROVADO), self.pareceres[0]
        )


def pode_parar_aqui(parecer: Parecer) -> bool:
    return parecer.veredito is not Veredito.ESCALONAR


def sem_ilegiveis(bilhetes: Sequence[ExtracaoBilhete]) -> tuple[ExtracaoBilhete, ...]:
    if not bilhetes:
        return (ExtracaoBilhete(ilegivel=True, confianca=0.0),)
    if len(bilhetes) == 1:
        return tuple(bilhetes)
    uteis = tuple(b for b in bilhetes if not b.ilegivel)
    return uteis or tuple(bilhetes[:1])


def conferir_cupons(
    bilhetes: Iterable[ExtracaoBilhete],
    *,
    confianca_minima: float = CONFIANCA_MINIMA,
    casas_do_link: Sequence[str] = (),
    odds_do_texto: Sequence[float] = (),
    postada_em: datetime | None = None,
) -> tuple[Parecer, ...]:
    return tuple(
        conferir(
            b.para_bilhete(),
            confianca_minima,
            casas_do_link,
            odds_do_texto,
            postada_em,
            Origem.IA,
        )
        for b in bilhetes
    )


def extrair(
    leitor: Leitor,
    imagem: bytes,
    tipo: TipoDeImagem = "image/jpeg",
    *,
    legenda: str = "",
    postada_em: datetime | None = None,
    casas_do_link: Iterable[str] = (),
    odds_do_texto: Iterable[float] = (),
    confianca_minima: float = CONFIANCA_MINIMA,
    cache: ExtracaoCache | None = None,
) -> Extraida:
    casas = tuple(casas_do_link)
    odds = tuple(odds_do_texto)
    chave = chave_de_imagem(imagem, legenda, postada_em=postada_em)

    guardada = None if cache is None else cache.buscar(chave)
    if guardada is None:
        leitura = leitor.ler(imagem, tipo, legenda=legenda, postada_em=postada_em)
        if cache is not None:
            cache.guardar(chave, leitura, substituir=False)
    else:
        leitura = Leitura(tuple(guardada.cupons), guardada.modelo)

    bilhetes = sem_ilegiveis(leitura.bilhetes)
    pareceres = conferir_cupons(
        bilhetes,
        confianca_minima=confianca_minima,
        casas_do_link=casas,
        odds_do_texto=odds,
        postada_em=postada_em,
    )
    veio_do_topo = guardada is not None and guardada.modelo == leitor.modelo_escalonamento
    if veio_do_topo or all(pode_parar_aqui(p) for p in pareceres):
        degrau = Degrau.BARATO if guardada is None else Degrau.CACHE
        return Extraida(bilhetes, pareceres, degrau, leitura.custo_usd)

    try:
        melhor = leitor.ler(imagem, tipo, legenda=legenda, postada_em=postada_em, escalonar=True)
    except LeituraFalhouError as erro:
        raise EscalonamentoFalhouError(leitura.custo_usd + erro.custo_usd) from erro
    except Exception as erro:
        raise EscalonamentoFalhouError(leitura.custo_usd) from erro
    if cache is not None:
        cache.guardar(chave, melhor)
    bilhetes = sem_ilegiveis(melhor.bilhetes)
    pareceres = conferir_cupons(
        bilhetes,
        confianca_minima=confianca_minima,
        casas_do_link=casas,
        odds_do_texto=odds,
        postada_em=postada_em,
    )
    return Extraida(bilhetes, pareceres, Degrau.CARO, leitura.custo_usd + melhor.custo_usd)
