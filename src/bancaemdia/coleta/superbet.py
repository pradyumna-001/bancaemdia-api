from typing import Any

from bancaemdia.coleta.leitura import (
    Coletada,
    ColetaInvalidaError,
    Escolha,
    centavos,
    data_que_conta,
    descricao_das_escolhas,
    instante_iso,
    mais_cedo,
    primeiro_evento,
    primeiro_mercado,
    tipo_da_aposta,
)

EM_ABERTO = ("open", "active", "pending")
SEM_BONUS = ("no_bonus", "none", "0")
TOLERANCIA = 0.001


def _escolhas(bruto: dict[str, Any]) -> list[Escolha]:
    # A estrutura é rasa: as escolhas moram em `events`, uma por evento, e a escolha é um objeto
    # único (`odd`), não uma lista — um leitor copiado da Betano não acharia nada.
    escolhas: list[Escolha] = []
    for evento in bruto.get("events") or []:
        if not isinstance(evento, dict):
            continue
        aposta_no = evento.get("odd") or {}
        mercado = evento.get("market") or {}

        nomes = [n for n in (evento.get("name") or []) if isinstance(n, str)]
        confronto = " x ".join(nomes) if nomes else None

        mercado_id = mercado.get("marketId")
        try:
            mercado_id = int(mercado_id) if mercado_id is not None else None
        except (TypeError, ValueError):
            mercado_id = None

        escolhas.append(
            Escolha(
                descricao=str(aposta_no.get("name") or "").strip(),
                mercado=(mercado.get("name") or None),
                mercado_id=mercado_id,
                linha=aposta_no.get("specialValue"),
                odd=aposta_no.get("coefficient"),
                resultado=aposta_no.get("oddStatus"),
                evento_id=str(evento.get("eventId") or "") or None,
                evento=confronto,
            )
        )
    return escolhas


def _o_bonus_explica(bruto: dict[str, Any], pago: float, ganho: float) -> str:
    # No bilhete real `891R-YIOQIQ` o `payoff` (217) era o `totalWinnings` (190) mais o
    # PROFIT_BOOST de 27, e o `bonusType` dizia "no_bonus": o bônus mora em `bonuses`.
    bonus = bruto.get("bonuses")
    if not isinstance(bonus, dict) or not bonus:
        return ""
    diferenca = float(pago) - float(ganho)
    for nome, dados in bonus.items():
        valor = (dados or {}).get("value") if isinstance(dados, dict) else None
        if isinstance(valor, (int, float)) and abs(float(valor) - diferenca) <= TOLERANCIA:
            return (
                f", e a diferença bate com o bônus {nome} de {valor:g}"
                " que a casa declarou nesta aposta"
            )
    return ""


def _resultado(bruto: dict[str, Any]) -> tuple[str, int | None, str | None]:
    win = bruto.get("win") or {}

    # O bilhete real do dono tem `status="win"` e `isCashedOut=true` ao mesmo tempo, e ele é
    # cashout: ler o `status` primeiro pagaria a aposta cheia numa que foi encerrada.
    if win.get("isCashedOut"):
        pago_cash = win.get("payoff")
        if not isinstance(pago_cash, (int, float)):
            return (
                "CASHOUT",
                None,
                "a casa marcou cashout mas não mandou o valor recebido — confira na casa",
            )
        return ("CASHOUT", centavos(pago_cash, "win.payoff"), None)

    estado_cru = str(bruto.get("status") or "").strip().lower()
    if not estado_cru or estado_cru in EM_ABERTO:
        return ("PENDENTE", None, None)

    if estado_cru == "lost":
        return ("RED", 0, None)

    if estado_cru == "refund":
        devolvido = win.get("payoff")
        if not isinstance(devolvido, (int, float)):
            return (
                "ANULADA",
                None,
                "a casa anulou esta aposta mas não mandou o valor devolvido — confira na casa",
            )
        return ("ANULADA", centavos(devolvido, "win.payoff"), None)

    # `"win"`, e não `"won"`: nas 55 apostas reais do dono veio `lost` 36, `win` 14, `refund` 1 e
    # `active` 4 — `won` nenhuma vez. Só entra o medido; o resto retém dizendo a palavra.
    if estado_cru == "win":
        pago = win.get("payoff")
        ganho = win.get("totalWinnings")
        if not isinstance(pago, (int, float)):
            return (
                "GREEN",
                None,
                "a casa disse que esta ganhou mas não mandou o valor pago — confira na casa",
            )
        # Dois números do mesmo retorno que discordam não viram escolha nossa: vale o `payoff` e a
        # aposta entra retida, mesmo quando um bônus explica a diferença (decisão do dono).
        if isinstance(ganho, (int, float)) and abs(float(ganho) - float(pago)) > TOLERANCIA:
            return (
                "GREEN",
                centavos(pago, "win.payoff"),
                f"a casa mandou dois números de retorno diferentes"
                f" (pago {pago}, ganho {ganho}){_o_bonus_explica(bruto, pago, ganho)}"
                " — confira na casa",
            )
        return ("GREEN", centavos(pago, "win.payoff"), None)

    return (
        "PENDENTE",
        None,
        f"a casa marcou esta aposta como {estado_cru!r}, que eu ainda não sei ler — confira e me"
        " diga o que ela foi",
    )


def ler(bruto: object) -> Coletada:
    if not isinstance(bruto, dict):
        raise ColetaInvalidaError("a aposta não veio como objeto")

    identidade = str(bruto.get("ticketId") or "").strip()
    if not identidade:
        raise ColetaInvalidaError("a aposta veio sem identificador (`ticketId`)")

    # A stake vem em REAIS: `payment.stake: 140` com odd 2,57 dá os 359,8 do `win.estimated`. A
    # BetMGM manda centavos no mesmo campo, e copiar um leitor no outro erraria por cem vezes.
    pagamento = bruto.get("payment") or {}
    stake = centavos(pagamento.get("stake"), "payment.stake")
    if stake <= 0:
        raise ColetaInvalidaError("a aposta veio sem valor apostado")

    colocada_em = instante_iso(bruto.get("dateReceived"), "dateReceived")

    escolhas = _escolhas(bruto)
    if not escolhas:
        raise ColetaInvalidaError("a aposta veio sem nenhuma escolha dentro")

    odd = bruto.get("coefficient")
    if not isinstance(odd, (int, float)) or odd <= 0:
        raise ColetaInvalidaError("a aposta veio sem a odd total")

    estado, retorno, motivo = _resultado(bruto)

    # Numa anulada a casa zera a odd (`coefficient: 1`, `initialCoefficient: 1.77` no bilhete
    # `891O-YNS3L2`). Só nela: com `priceBoost` o `initialCoefficient` é a odd antiga, 2,12 e não
    # 2,57.
    if estado == "ANULADA":
        anterior = bruto.get("initialCoefficient")
        if isinstance(anterior, (int, float)) and anterior > odd:
            odd = anterior

    # Aposta de sistema tem combinações parciais: a odd total e o retorno não seguem a régua de
    # sempre, e o produto não sabe contá-la.
    if bruto.get("system") and not motivo:
        motivo = (
            "esta é uma aposta de SISTEMA (combinações parciais), e eu ainda não sei contar o"
            " retorno dela — confira na casa"
        )

    # Bônus desconhecido não vira freebet por dedução. Aqui o "sem bônus" é o texto `"no_bonus"`,
    # medido.
    bonus = pagamento.get("bonusType")
    if bonus and str(bonus).lower() not in SEM_BONUS and not motivo:
        motivo = (
            f"a casa marcou um bônus nesta aposta (bonusType={bonus!r}) e eu ainda não sei o que ele"
            " significa — confira se ela saiu do seu bolso"
        )

    comeca = mais_cedo([
        instante_iso(evento["date"], "events[].date")
        for evento in (bruto.get("events") or [])
        if isinstance(evento, dict) and evento.get("date")
    ])

    return Coletada(
        identidade=identidade,
        casa="superbet",
        tipo=tipo_da_aposta(escolhas),
        odd=float(odd),
        stake_centavos=stake,
        data_aposta=data_que_conta(comeca, colocada_em),
        colocada_em=colocada_em,
        escolhas=tuple(escolhas),
        evento=primeiro_evento(escolhas),
        mercado_bruto=primeiro_mercado(escolhas),
        descricao=descricao_das_escolhas(escolhas),
        comeca_em=comeca,
        estado=estado,
        retorno_centavos=retorno,
        motivo_retencao=motivo,
        bruto=bruto,
    )
