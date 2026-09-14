from typing import Literal

from pydantic import BaseModel, Field

from bancaemdia.domain import conferencias


class Selecao(BaseModel):
    mercado: str
    escolha: str
    linha: float | None = None
    odd: float | None = None
    evento: str | None = None


class ExtracaoBilhete(BaseModel):
    casa: str | None = None
    tipo: Literal["simples", "multipla", "criar_aposta", "sistema"] = "simples"
    evento: str | None = None
    selecoes: list[Selecao] = Field(default_factory=list)
    odd_total: float | None = None
    odd_original: float | None = None
    quando: str | None = None
    ilegivel: bool = False
    confianca: float = 0.0

    def para_bilhete(self) -> conferencias.Bilhete:
        return conferencias.Bilhete(
            casa=self.casa,
            tipo=conferencias.TipoBilhete(self.tipo.upper()),
            evento=self.evento,
            selecoes=tuple(
                conferencias.Selecao(
                    mercado=s.mercado,
                    escolha=s.escolha,
                    linha=s.linha,
                    odd=s.odd,
                    evento=s.evento,
                )
                for s in self.selecoes
            ),
            odd_total=self.odd_total,
            odd_original=self.odd_original,
            quando=self.quando,
            ilegivel=self.ilegivel,
            confianca=self.confianca,
        )


class Cupons(BaseModel):
    cupons: list[ExtracaoBilhete] = Field(default_factory=list)
