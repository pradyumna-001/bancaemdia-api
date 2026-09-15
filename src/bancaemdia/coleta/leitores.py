from collections.abc import Callable

from bancaemdia.coleta import altenar, betano, betfair, betmgm, kambi, superbet
from bancaemdia.coleta.leitura import Coletada

Leitor = Callable[[object], Coletada]

PLATAFORMAS: dict[str, Leitor] = {
    "betano": betano.ler,
    "superbet": superbet.ler,
    "betmgm": betmgm.ler,
    "betfair": betfair.ler,
    "kambi": kambi.ler,
    "altenar": altenar.ler,
}
# Casa quase nunca escreve o próprio software: a KTO roda em Kambi e a Esportiva em Altenar, e
# uma plataforma atende várias marcas com o mesmo JSON. Casa nova numa plataforma conhecida é uma
# linha aqui, e só depois de uma amostra dela lida de verdade.
PLATAFORMA_DA_CASA: dict[str, str] = {
    "betano": "betano",
    "superbet": "superbet",
    "betmgm": "betmgm",
    "betfair": "betfair",
    "kto": "kambi",
    "esportiva": "altenar",
}
LEITORES: dict[str, Leitor] = {
    casa: PLATAFORMAS[plataforma]
    for casa, plataforma in PLATAFORMA_DA_CASA.items()
    if plataforma in PLATAFORMAS
}
