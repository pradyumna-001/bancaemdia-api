import json
from typing import Any

from bancaemdia.coleta.leitura import (
    Coletada,
    ColetaInvalidaError,
    Escolha,
    centavos,
    data_que_conta,
    descricao_das_escolhas,
    instante_iso,
    primeiro_evento,
    primeiro_mercado,
    tipo_da_aposta,
)

MARCAS_DE_BONUS = ("bonusMode", "bonusPart", "bonusPartPercent", "initBonusPart")


def _escolhas(bruto: dict[str, Any]) -> list[Escolha]:
    achadas: list[Escolha] = []
    for escolha in bruto.get("selections") or []:
        if not isinstance(escolha, dict):
            continue
        # A linha vem num JSON dentro de um texto (`spec`: {"1": "27.5"}) e é enfeite do mercado,
        # não dinheiro: se não der para ler, segue sem ela em vez de derrubar a aposta inteira.
        try:
            valores = json.loads(escolha.get("spec") or "{}")
            primeiro = next(iter(valores.values()), None)
            linha = float(primeiro) if primeiro is not None else None
        except (ValueError, TypeError, AttributeError):
            linha = None
        preco = escolha.get("price")
        achadas.append(
            Escolha(
                descricao=str(escolha.get("name") or "").strip(),
                mercado=str(escolha["marketName"]).strip() if escolha.get("marketName") else None,
                linha=linha,
                odd=(
                    float(preco)
                    if isinstance(preco, (int, float)) and not isinstance(preco, bool)
                    else None
                ),
                evento_id=str(escolha["eventId"]) if escolha.get("eventId") is not None else None,
                evento=str(escolha["eventName"]).strip() if escolha.get("eventName") else None,
            )
        )
    return achadas


def _resultado(bruto: dict[str, Any]) -> tuple[str, int | None, str | None]:
    estado = bruto.get("status")

    # O `totalWin` vem preenchido na aposta em curso com o POTENCIAL, `totalStake` vezes
    # `totalOdds` nas três medidas de `status: 0`: lido como retorno, toda aposta correndo entraria
    # GREEN paga.
    if estado == 0:
        return ("PENDENTE", None, None)
    if estado == 2:
        return ("RED", 0, None)
    if estado == 1:
        pago = bruto.get("totalWin")
        if not isinstance(pago, (int, float)) or isinstance(pago, bool):
            return (
                "PENDENTE",
                None,
                "a casa liquidou esta aposta mas não mandou o valor pago — confira na casa",
            )
        return ("GREEN", centavos(pago, "totalWin"), None)

    return (
        "PENDENTE",
        None,
        f"a casa marcou esta aposta com status {estado!r}, que eu ainda não sei ler — os medidos"
        " foram 0 (em curso), 1 (liquidada) e 2 (perdida). Confira e me diga o que ela foi",
    )


def _bonus(bruto: dict[str, Any]) -> str | None:
    # Uma das 22 medidas era freebet (`bonusPartPercent: 100`) e pelo JSON parecia uma ganha que
    # pagou pouco. A `Coletada` não sabe gravar freebet vinda da casa, e bônus parcial ninguém viu:
    # qualquer marca retém.
    marcas = {campo: bruto.get(campo) for campo in MARCAS_DE_BONUS if bruto.get(campo)}
    if not marcas:
        return None
    return (
        f"a casa marcou BÔNUS nesta aposta ({marcas}) — pode ser freebet, e o planilhador ainda não"
        " sabe contá-la vinda da casa. Ela fica fora do ROI até você conferir se o dinheiro saiu do"
        " seu bolso"
    )


def ler(bruto: object) -> Coletada:
    if not isinstance(bruto, dict):
        raise ColetaInvalidaError("a aposta não veio como objeto")

    identidade = str(bruto.get("id") or "").strip()
    if not identidade:
        raise ColetaInvalidaError("a aposta veio sem identificador (`id`)")

    moeda = bruto.get("currency")
    if moeda and moeda != "BRL":
        raise ColetaInvalidaError(f"esta aposta está em {moeda}, e o planilhador conta em reais")

    # O dinheiro vem em reais, e o JSON prova sozinho: o `totalWin` traz decimais de centavo
    # (77.5, 148.23, 167.06), e casa nenhuma paga fração de centavo.
    stake = bruto.get("totalStake")
    if not isinstance(stake, (int, float)) or isinstance(stake, bool) or stake <= 0:
        raise ColetaInvalidaError("a aposta veio sem valor apostado")

    odd = bruto.get("totalOdds")
    if not isinstance(odd, (int, float)) or isinstance(odd, bool) or odd <= 0:
        raise ColetaInvalidaError("a aposta veio sem a odd total (`totalOdds`)")

    escolhas = _escolhas(bruto)
    if not escolhas:
        raise ColetaInvalidaError("a aposta veio sem nenhuma escolha dentro")

    colocada_em = instante_iso(bruto.get("createdDate"), "createdDate")
    estado, retorno, motivo = _resultado(bruto)

    # O bônus retém mesmo quando o resultado foi lido sem problema: é justamente a aposta que
    # "leu bem" que envenenaria o ROI.
    motivo = motivo or _bonus(bruto)

    # Cashout não foi medido: `cashOutValue` é 0 e `partialCashouts` é vazio nas 22. Campo com um
    # valor só não é campo medido; se vier diferente, retém em vez de virar palpite.
    if not motivo and (
        bruto.get("cashOutValue") or bruto.get("partialCashOut") or bruto.get("partialCashouts")
    ):
        motivo = (
            "a casa marcou cashout nesta aposta e eu ainda não sei ler o formato dele nesta"
            " plataforma — confira na casa"
        )

    # O histórico da Altenar não traz o instante do jogo, só o `eventName` e o `eventId`: é a única
    # plataforma em que a data que conta cai na colocação.
    return Coletada(
        identidade=identidade,
        casa="altenar",
        tipo=tipo_da_aposta(escolhas),
        odd=float(odd),
        stake_centavos=centavos(stake, "totalStake"),
        data_aposta=data_que_conta(None, colocada_em),
        colocada_em=colocada_em,
        escolhas=tuple(escolhas),
        evento=primeiro_evento(escolhas),
        mercado_bruto=primeiro_mercado(escolhas),
        descricao=descricao_das_escolhas(escolhas),
        comeca_em=None,
        estado=estado,
        retorno_centavos=retorno,
        motivo_retencao=motivo,
        bruto=bruto,
    )
