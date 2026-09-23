from typing import Any

from bancaemdia.coleta.leitura import (
    Coletada,
    ColetaInvalidaError,
    Escolha,
    data_que_conta,
    descricao_das_escolhas,
    instante_iso,
    mais_cedo,
    primeiro_evento,
    primeiro_mercado,
    tipo_da_aposta,
)

ESTADOS_EM_CURSO = ("OPEN", "PENDING", "PLACED")


def _odd_texto(valor: object) -> float | None:
    # Texto que não converte volta `None` e quem chamou retém, nunca zero: uma odd zero passaria
    # calada pelas contas e mudaria o retorno calculado.
    if isinstance(valor, (int, float)):
        return float(valor)
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        return float(valor.strip())
    except ValueError:
        return None


def _escolhas(bruto: dict[str, Any]) -> list[Escolha]:
    escolhas: list[Escolha] = []
    for evento in bruto.get("events") or []:
        if not isinstance(evento, dict):
            continue

        # Em aposta de longo prazo (`OUTRIGHT`) os participantes vêm vazios e o nome é genérico
        # ("Longo Prazo"): o confronto real só aparece no texto do mercado, e tirá-lo dali seria
        # adivinhar.
        nomes: list[Any] = [
            p.get("name")
            for p in (evento.get("participants") or [])
            if isinstance(p, dict) and p.get("name")
        ]
        confronto = " x ".join(nomes) if nomes else (evento.get("name") or None)
        evento_id = str(evento.get("id") or "") or None

        for escolha in evento.get("outcomes") or []:
            if not isinstance(escolha, dict):
                continue
            mercado = escolha.get("marketType") or {}
            escolhas.append(
                Escolha(
                    descricao=str(escolha.get("name") or "").strip(),
                    mercado=(mercado.get("name") or None),
                    odd=_odd_texto(escolha.get("odds")),
                    resultado=escolha.get("status"),
                    evento_id=evento_id,
                    evento=confronto,
                )
            )
    return escolhas


def _resultado(aposta: dict[str, Any]) -> tuple[str, int | None, str | None]:
    estados = aposta.get("displayStatuses") or []
    if not isinstance(estados, list) or not estados:
        return ("PENDENTE", None, None)

    # Mais de um estado no array não vira escolha nossa: qual deles é o dinheiro? Reter é o lado
    # seguro.
    if len(estados) > 1:
        return (
            "PENDENTE",
            None,
            "a casa marcou esta aposta com mais de um estado ao mesmo tempo"
            f" ({', '.join(map(str, estados))}) e eu não sei qual vale — confira na casa",
        )

    estado = str(estados[0]).strip().upper()
    # O `payout` já vem em centavos, como a stake; ausente continua ausente em qualquer unidade.
    cru = aposta.get("payout")
    pago = int(cru) if isinstance(cru, (int, float)) else None

    if aposta.get("cashoutHistory"):
        if pago is None:
            return (
                "CASHOUT",
                None,
                "a casa marcou cashout mas não mandou o valor recebido — confira na casa",
            )
        return ("CASHOUT", pago, None)
    if estado == "LOST":
        return ("RED", 0, None)
    if estado == "WON":
        if pago is None:
            return (
                "GREEN",
                None,
                "a casa disse que esta ganhou mas não mandou o valor pago — confira na casa",
            )
        return ("GREEN", pago, None)
    if estado in ESTADOS_EM_CURSO:
        return ("PENDENTE", None, None)
    return (
        "PENDENTE",
        None,
        f"a casa marcou esta aposta como {estado!r}, que eu ainda não sei ler — confira e me diga"
        " o que ela foi",
    )


def ler(bruto: object) -> Coletada:
    if not isinstance(bruto, dict):
        raise ColetaInvalidaError("a aposta não veio como objeto")

    identidade = str(bruto.get("uuid") or "").strip()
    if not identidade:
        raise ColetaInvalidaError("a aposta veio sem identificador (`uuid`)")

    moeda = bruto.get("currency")
    if moeda and moeda != "BRL":
        raise ColetaInvalidaError(f"esta aposta está em {moeda}, e o planilhador conta em reais")

    # Um bilhete pode carregar várias apostas, e o produto conta uma por identidade: dividir o
    # `uuid` entre elas seria inventar identidade.
    apostas = [b for b in (bruto.get("bets") or []) if isinstance(b, dict)]
    if not apostas:
        raise ColetaInvalidaError("a aposta veio sem nenhuma aposta dentro (`bets`)")

    aposta = apostas[0]
    motivo_lote = None
    if len(apostas) > 1:
        motivo_lote = (
            f"este bilhete tem {len(apostas)} apostas dentro e eu ainda não sei separá-las"
            " — confira na casa"
        )

    # A stake já vem em centavos, e esta é a única casa assim: `8000` é R$ 80,00, conferido pelo
    # dono na tela da BetMGM, porque nenhuma conta do JSON revelava a unidade.
    stake = aposta.get("stake")
    if not isinstance(stake, (int, float)) or stake <= 0:
        raise ColetaInvalidaError("a aposta veio sem valor apostado")
    stake_centavos = int(stake)

    colocada_em = instante_iso(bruto.get("deliveryDate"), "deliveryDate")

    escolhas = _escolhas(bruto)
    if not escolhas:
        raise ColetaInvalidaError("a aposta veio sem nenhuma escolha dentro")

    odd = _odd_texto(aposta.get("totalOdds"))
    if odd is None or odd <= 0:
        raise ColetaInvalidaError("a aposta veio sem a odd total")

    estado, retorno, motivo = _resultado(aposta)
    motivo = motivo or motivo_lote

    comeca = mais_cedo([
        instante_iso(evento["startsAt"], "events[].startsAt")
        for evento in (bruto.get("events") or [])
        if isinstance(evento, dict) and evento.get("startsAt")
    ])

    return Coletada(
        identidade=identidade,
        casa="betmgm",
        tipo=tipo_da_aposta(escolhas),
        odd=odd,
        stake_centavos=stake_centavos,
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
