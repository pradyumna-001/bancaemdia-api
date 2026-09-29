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
    identidade_bilhete: str | None = Field(default=None, min_length=1, max_length=200)
    ocorrido_em: str | None = None
    stake_unidades: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    conta_casa_ref: int | None = Field(default=None, ge=1, le=2**63 - 1)

    def para_bilhete(self) -> conferencias.Bilhete:
        return conferencias.Bilhete(
            casa=self.casa,
            tipo=conferencias.TipoBilhete(self.tipo),
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
            identidade_bilhete=self.identidade_bilhete,
            ocorrido_em=self.ocorrido_em,
            stake_unidades=self.stake_unidades,
            conta_casa_ref=self.conta_casa_ref,
        )


class Cupons(BaseModel):
    cupons: list[ExtracaoBilhete] = Field(default_factory=list)
