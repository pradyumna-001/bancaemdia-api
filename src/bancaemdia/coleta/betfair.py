from typing import Any

from bancaemdia.coleta.leitura import (
    Coletada,
    ColetaInvalidaError,
    Escolha,
    centavos,
    descricao_das_escolhas,
    instante_iso,
    primeiro_evento,
    primeiro_mercado,
    tipo_da_aposta,
)


def _visitar(objeto: object, achados: dict[str, Any]) -> None:
    if isinstance(objeto, list):
        for item in objeto:
            _visitar(item, achados)
        return
    if not isinstance(objeto, dict):
        return
    tipo = objeto.get("__typename")
    if tipo == "SportsbookBetCard" and objeto.get("bet"):
        achados["bet"] = objeto["bet"]
    elif tipo == "BetLegCard" and objeto.get("leg"):
        for parte in objeto["leg"].get("parts") or []:
            if isinstance(parte, dict):
                achados["partes"].append(parte)
    elif tipo == "FixtureCard":
        achados["fixture"] = achados["fixture"] or objeto.get("fixture")
        achados["sportevent"] = achados["sportevent"] or objeto.get("sportevent")
    for valor in objeto.values():
        if isinstance(valor, (dict, list)):
            _visitar(valor, achados)


def _cartoes(bruto: dict[str, Any]) -> dict[str, Any]:
    # Os cartões são achados pelo `__typename`, nunca pela posição: a ordem deles é decisão de tela
    # da casa e muda sem avisar, e o nome do tipo é contrato do GraphQL.
    achados: dict[str, Any] = {"bet": None, "partes": [], "fixture": None, "sportevent": None}
    _visitar(bruto, achados)
    return achados


def _acabou(aposta: dict[str, Any]) -> tuple[bool, str | None]:
    # O `result` desta casa é vivo (`WINNING` numa aposta correndo) e não marca fim: ligado ao
    # `ja_acabou`, virou cashout de R$ 1,60 numa múltipla em curso. Nas sete apostas medidas,
    # `resultType` e `isSettled` sempre concordaram; se discordarem, ninguém escolhe.
    confirmado = str(aposta.get("resultType") or "").strip().upper() == "CONFIRMED"
    liquidada = bool(aposta.get("isSettled"))
    if confirmado == liquidada:
        return (confirmado, None)
    return (
        False,
        f"a casa marcou esta aposta como {aposta.get('resultType')!r} e ao mesmo tempo"
        f" isSettled={liquidada!r} — os dois marcadores de fim discordam e eu não escolho por conta"
        " própria; confira na casa",
    )


def _escolhas(cartoes: dict[str, Any]) -> list[Escolha]:
    fixture = cartoes.get("fixture") or {}
    casa_time = (fixture.get("home") or {}).get("name")
    fora_time = (fixture.get("away") or {}).get("name")
    confronto = f"{casa_time} x {fora_time}" if casa_time and fora_time else None
    do_cartao = str((cartoes.get("sportevent") or {}).get("eventId") or "") or None

    escolhas: list[Escolha] = []
    for parte in cartoes.get("partes") or []:
        preco = parte.get("price") or {}
        urn = str(parte.get("eventUrn") or "")
        escolhas.append(
            Escolha(
                descricao=str(parte.get("selectionName") or "").strip(),
                mercado=parte.get("eventMarketDescription") or None,
                linha=parte.get("handicap"),
                odd=preco.get("decimal"),
                # Cada perna traz o SEU jogo: o do cartão, carimbado em todas, apagava o segundo
                # jogo da múltipla e a fazia CRIAR_APOSTA. O cartão fica só como reserva.
                evento_id=(urn.rsplit(":", 1)[-1] if urn else None) or do_cartao,
                evento=parte.get("eventDescription") or confronto,
            )
        )
    return escolhas


def _odd(aposta: dict[str, Any], escolhas: list[Escolha]) -> tuple[float, str | None]:
    # A odd total vem em `betPrice.decimal` enquanto a aposta corre e some quando ela liquida. Sem
    # ela, só a de uma escolha é confiável: multiplicar por conta própria é o número bom demais.
    declarada = (aposta.get("betPrice") or {}).get("decimal")
    odds = [e.odd for e in escolhas if isinstance(e.odd, (int, float))]
    if isinstance(declarada, (int, float)) and declarada > 0:
        return (float(declarada), None)
    if len(escolhas) == 1 and odds:
        return (float(odds[0]), None)
    if odds:
        return (
            float(odds[0]),
            f"esta aposta tem {len(escolhas)} escolhas e a casa não mandou a odd total — não"
            " multiplico por conta própria. Confira na casa",
        )
    raise ColetaInvalidaError("a aposta veio sem a odd")


def _fecha(pago: int, esperado: int) -> bool:
    # Folga de 1% para o arredondamento da casa, com piso de 1 centavo para a aposta pequena não
    # ficar sem tolerância nenhuma.
    return abs(pago - esperado) <= max(1, round(esperado * 0.01))


def _retorno(pago: object, stake: int, odd: float) -> tuple[int | None, str | None]:
    # O `profitAndLoss` veio 0 em duas perdidas medidas: é o retorno ou o lucro com piso zero. Numa
    # ganha as duas contas só coincidem com odd perto de 100 ou mais, e aí vale a de retorno. Quando
    # nenhuma fecha (cashout parcial, dead heat, Rule 4, bônus), ninguém adivinha.
    if not isinstance(pago, (int, float)):
        return (None, "a casa disse que esta ganhou mas não mandou o valor — confira na casa")

    pago_centavos = round(float(pago) * 100)
    como_retorno = round(stake * odd)
    como_lucro = round(stake * (odd - 1))
    if _fecha(pago_centavos, como_retorno):
        return (pago_centavos, None)
    if _fecha(pago_centavos, como_lucro):
        return (pago_centavos + stake, None)
    return (
        pago_centavos,
        f"a casa pagou {pago} nesta aposta, e isso não bate nem com o retorno esperado"
        f" ({como_retorno / 100:.2f}) nem com o lucro ({como_lucro / 100:.2f}) — pode ser cashout,"
        " dead heat ou bônus. Confira na casa",
    )


def _resultado(
    aposta: dict[str, Any], stake: int, odd: float
) -> tuple[str, int | None, str | None]:
    resultado = str(aposta.get("result") or "").strip().upper()
    acabou, motivo = _acabou(aposta)
    if not acabou:
        return ("PENDENTE", None, motivo)

    # O `cashoutQuote` é a cotação de recompra, e ela existe enquanto a aposta corre: perguntada
    # antes do fim, fazia a aposta em curso entrar como cashout.
    if aposta.get("cashoutQuote"):
        pago = aposta.get("profitAndLoss")
        if isinstance(pago, (int, float)):
            return ("CASHOUT", centavos(pago, "bet.profitAndLoss"), None)
        return (
            "CASHOUT",
            None,
            "a casa marcou cashout mas não mandou o valor recebido — confira na casa",
        )
    if not resultado or resultado in ("OPEN", "PENDING"):
        return ("PENDENTE", None, None)
    if resultado == "LOST":
        return ("RED", 0, None)
    if resultado == "WON":
        retorno, motivo = _retorno(aposta.get("profitAndLoss"), stake, odd)
        return ("GREEN", retorno, motivo)
    return (
        "PENDENTE",
        None,
        f"a casa marcou esta aposta como {resultado!r}, que eu ainda não sei ler — confira e me"
        " diga o que ela foi",
    )


def ler(bruto: object) -> Coletada:
    if not isinstance(bruto, dict):
        raise ColetaInvalidaError("a aposta não veio como objeto")

    cartoes = _cartoes(bruto)
    aposta = cartoes.get("bet")
    if not isinstance(aposta, dict):
        raise ColetaInvalidaError("não achei o cartão da aposta (`SportsbookBetCard`) neste grupo")

    identidade = str(aposta.get("id") or "").strip()
    if not identidade:
        raise ColetaInvalidaError("a aposta veio sem identificador (`bet.id`)")

    stake = centavos(aposta.get("currentSize"), "bet.currentSize")
    if stake <= 0:
        raise ColetaInvalidaError("a aposta veio sem valor apostado")

    escolhas = _escolhas(cartoes)
    if not escolhas:
        raise ColetaInvalidaError("a aposta veio sem nenhuma escolha dentro")

    # A casa já conta pelo jogo: `lowestEventStartTime` é o jogo mais cedo da aposta, e deixar o
    # `comeca_em` vazio fazia só esta casa calar o campo que as outras preenchem.
    comeca = instante_iso(aposta.get("lowestEventStartTime"), "bet.lowestEventStartTime")

    odd, motivo = _odd(aposta, escolhas)

    # `numLines > 1` é aposta com várias linhas, cada uma com stake própria: o produto conta uma, e
    # dividir seria inventar.
    linhas = aposta.get("numLines")
    if isinstance(linhas, (int, float)) and linhas > 1 and not motivo:
        motivo = (
            f"esta aposta tem {int(linhas)} linhas dentro e eu ainda não sei separá-las — confira"
            " na casa"
        )

    estado, retorno, motivo_do_resultado = _resultado(aposta, stake, odd)
    motivo = motivo or motivo_do_resultado

    placar = (cartoes.get("fixture") or {}).get("score") or {}
    observacao = None
    if isinstance(placar, dict) and placar.get("home") is not None:
        observacao = f"Resultado {placar.get('home')}-{placar.get('away')}"

    return Coletada(
        identidade=identidade,
        casa="betfair",
        tipo=tipo_da_aposta(escolhas),
        odd=odd,
        stake_centavos=stake,
        data_aposta=comeca,
        # This reader has no verified placement timestamp. Leave attribution for review.
        colocada_em=None,
        escolhas=tuple(escolhas),
        evento=primeiro_evento(escolhas),
        mercado_bruto=primeiro_mercado(escolhas),
        descricao=descricao_das_escolhas(escolhas),
        comeca_em=comeca,
        estado=estado,
        retorno_centavos=retorno,
        observacao=observacao,
        motivo_retencao=motivo,
        bruto=bruto,
    )
