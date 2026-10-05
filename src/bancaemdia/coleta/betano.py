from typing import Any

from bancaemdia.coleta.leitura import (
    Coletada,
    ColetaInvalidaError,
    Escolha,
    centavos,
    data_que_conta,
    descricao_das_escolhas,
    instante,
    ja_acabou,
    mais_cedo,
    primeiro_evento,
    primeiro_mercado,
    tipo_da_aposta,
)


def _escolhas(bruto: dict[str, Any]) -> list[Escolha]:
    # São quatro níveis (bets → legs → legItems → selections), e o evento mora no legItem.
    achadas: list[Escolha] = []
    for perna in bruto.get("legs") or []:
        for item in perna.get("legItems") or []:
            evento_id = item.get("eventId")
            evento = item.get("eventName")
            for selecao in item.get("selections") or []:
                descricao = (selecao.get("description") or "").strip()
                if not descricao:
                    raise ColetaInvalidaError(
                        "uma das escolhas veio sem descrição — não dá para dizer o que foi apostado"
                    )
                achadas.append(
                    Escolha(
                        descricao=descricao,
                        mercado=(selecao.get("market") or "").strip() or None,
                        mercado_id=selecao.get("marketTypeId"),
                        linha=selecao.get("handicap"),
                        odd=selecao.get("odds"),
                        resultado=selecao.get("result"),
                        evento_id=str(evento_id) if evento_id is not None else None,
                        evento=(evento or "").strip() or None,
                    )
                )
    return achadas


def _resultado(bruto: dict[str, Any]) -> tuple[str, int | None, str | None]:
    if not ja_acabou(bruto):
        return ("PENDENTE", None, None)
    final = bruto.get("finalBetResult")
    if not final:
        return ("PENDENTE", None, None)

    # Ausente não é zero: uma aposta ganha sem o valor pago virava GREEN com retorno zero, a stake
    # perdida gravada calada. Só a falta retém; um zero que a casa mandou continua valendo.
    cru = bruto.get("finalWinnings")
    pago = centavos(cru, "finalWinnings") if isinstance(cru, (int, float)) else None

    if final == "Win":
        if pago is None:
            return (
                "GREEN",
                None,
                "a casa disse que esta ganhou mas não mandou o valor pago — confira na casa",
            )
        return ("GREEN", pago, None)
    if final == "Lose":
        return ("RED", 0, None)
    if final == "Cashout":
        # O cashout vem no `finalWinnings`: num encerramento real de R$ 1,00 os campos
        # `cashoutStatus` e `cashoutAmount` vieram zerados, iguais aos de quem nunca teve cashout.
        if pago is None:
            return (
                "CASHOUT",
                None,
                "a casa marcou cashout mas não mandou o valor recebido — confira na casa",
            )
        return ("CASHOUT", pago, None)
    if final == "Void":
        if pago is None:
            return (
                "ANULADA",
                None,
                "a casa anulou esta aposta mas não mandou o valor devolvido — confira na casa",
            )
        return ("ANULADA", pago, None)
    return (
        "PENDENTE",
        None,
        f"a casa marcou esta aposta como {final!r}, que eu ainda não sei ler — confira e me diga"
        " o que ela foi",
    )


def ler(bruto: object) -> Coletada:
    if not isinstance(bruto, dict):
        raise ColetaInvalidaError("a aposta não veio como objeto")

    # A identidade é o `id` do bilhete, nunca o `betslipId`: esse é o carrinho, e juntaria apostas
    # diferentes feitas no mesmo clique.
    identidade = str(bruto.get("id") or "").strip()
    if not identidade:
        raise ColetaInvalidaError("a aposta veio sem identificador (`id`)")

    moeda = (bruto.get("totalAmountWithCurrency") or {}).get("currencyCode")
    if moeda and moeda != "BRL":
        raise ColetaInvalidaError(f"esta aposta está em {moeda}, e o planilhador conta em reais")

    stake = centavos(bruto.get("totalAmount"), "totalAmount")
    if stake <= 0:
        raise ColetaInvalidaError("a aposta veio sem valor apostado")

    colocada_em = instante(bruto.get("placedAt"), "placedAt")

    escolhas = _escolhas(bruto)
    if not escolhas:
        raise ColetaInvalidaError("a aposta veio sem nenhuma escolha dentro")

    estado, retorno, motivo = _resultado(bruto)

    # Bônus desconhecido não vira freebet por dedução: freebet zera a stake nas contas. O
    # `bonusType: 0` é "sem bônus", medido; qualquer outro valor entra retido.
    bonus = bruto.get("bonusType") or 0
    if bonus and not motivo:
        motivo = (
            f"a casa marcou um bônus nesta aposta (bonusType={bonus}) e eu ainda não sei o que ele"
            " significa — confira se ela saiu do seu bolso"
        )

    odd = bruto.get("totalOdds")
    if not isinstance(odd, (int, float)) or odd <= 0:
        raise ColetaInvalidaError("a aposta veio sem a odd total")

    evento = primeiro_evento(escolhas)
    comeca = mais_cedo([
        instante(item["startTime"], "startTime")
        for perna in (bruto.get("legs") or [])
        for item in (perna.get("legItems") or [])
        if item.get("startTime")
    ])

    placar = None
    for perna in bruto.get("legs") or []:
        for item in perna.get("legItems") or []:
            if item.get("finalScoreResult"):
                placar = item["finalScoreResult"]
                break
        if placar:
            break

    return Coletada(
        identidade=identidade,
        casa="betano",
        tipo=tipo_da_aposta(escolhas),
        odd=float(odd),
        stake_centavos=stake,
        data_aposta=data_que_conta(comeca, colocada_em),
        colocada_em=colocada_em,
        escolhas=tuple(escolhas),
        evento=evento,
        mercado_bruto=primeiro_mercado(escolhas),
        descricao=descricao_das_escolhas(escolhas),
        comeca_em=comeca,
        estado=estado,
        retorno_centavos=retorno,
        observacao=placar,
        motivo_retencao=motivo,
        bruto=bruto,
    )
