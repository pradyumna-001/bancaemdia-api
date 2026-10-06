USD_PARA_BRL = 5.40

# Medido no banco limpo de junho/2026, com o Sonnet a US$ 3/15 por milhão de tokens: US$ 1,513531 ÷
# 279 bilhetes, cada um lido uma vez pelo Haiku e 20,4% subindo para o Sonnet. Os números anteriores
# erravam de 30% para baixo a 86% para cima, e o preço serve para a pessoa decidir se relê.
USD_POR_BILHETE_REFERENCIA = 0.0054
ORIGEM_DA_REFERENCIA = "referência medida em 279 bilhetes limpos"


def em_reais(usd: float) -> float:
    return usd * USD_PARA_BRL
