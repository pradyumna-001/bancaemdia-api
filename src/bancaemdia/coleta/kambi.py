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

# A Kambi escreve `stake: 80000` para R$ 80,00, `playedOdds: 1850` para a odd 1,85 e `line: 500`
# para a linha 0,5. O JSON não prova a unidade, porque stake e payout estão na mesma escala: quem
# respondeu foi o dono, com a tela da KTO aberta, em 17/08/2026.
MILESIMOS_POR_CENTAVO = 10


def _milesimos(valor: object, campo: str) -> int:
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        raise ColetaInvalidaError(f"o campo {campo} não veio como número: {valor!r}")
    return round(valor / MILESIMOS_POR_CENTAVO)


def _milhar(valor: object) -> float | None:
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        return None
    return valor / 1000


def _escolhas(bruto: dict[str, Any]) -> list[Escolha]:
    ofertas = {
        o.get("betOfferId"): o for o in (bruto.get("betOffers") or []) if isinstance(o, dict)
    }
    jogos = {e.get("eventId"): e for e in (bruto.get("events") or []) if isinstance(e, dict)}

    # Num BET_BUILDER a casa dá uma odd combinada e não publica a de cada perna: a odd da escolha
    # só sai da linha do cupom que aponta uma escolha, e fora dela fica None em vez de inventada.
    odd_da_escolha: dict[object, float | None] = {}
    for linha_do_cupom in bruto.get("couponRows") or []:
        if isinstance(linha_do_cupom, dict) and linha_do_cupom.get("outcomeId") is not None:
            odd_da_escolha[linha_do_cupom["outcomeId"]] = _milhar(linha_do_cupom.get("playedOdds"))

    achadas: list[Escolha] = []
    for escolha in bruto.get("outcomes") or []:
        if not isinstance(escolha, dict):
            continue
        oferta = ofertas.get(escolha.get("betOfferId")) or {}
        jogo = jogos.get(escolha.get("eventId")) or {}
        linha = escolha.get("line")
        if linha is None:
            linha = oferta.get("line")
        achadas.append(
            Escolha(
                descricao=str(escolha.get("label") or "").strip(),
                mercado=str(oferta.get("criterion")).strip() if oferta.get("criterion") else None,
                linha=_milhar(linha),
                odd=odd_da_escolha.get(escolha.get("outcomeId")),
                resultado=(
                    str(escolha.get("status")).strip().upper() if escolha.get("status") else None
                ),
                evento_id=str(escolha["eventId"]) if escolha.get("eventId") is not None else None,
                evento=str(jogo.get("eventName")).strip() if jogo.get("eventName") else None,
            )
        )
    return achadas


def _resultado(aposta: dict[str, Any]) -> tuple[str, int | None, str | None]:
    estado = str(aposta.get("betStatus") or "").strip().upper()
    pago = aposta.get("payout")

    if estado == "OPEN":
        # Medido: o cupom em aberto veio `payout: 0` com `potentialPayout` preenchido. Esse zero é
        # "ainda não pagou", e gravá-lo diria que a aposta perdeu.
        return ("PENDENTE", None, None)
    if estado == "LOST":
        return ("RED", 0, None)
    if estado in ("WON", "VOID"):
        if not isinstance(pago, (int, float)) or isinstance(pago, bool):
            # Ausente não é zero: uma ganha sem valor pago é leitura incompleta, não uma ganha que
            # pagou nada.
            return (
                "PENDENTE",
                None,
                f"a casa marcou esta aposta como {estado} mas não mandou o valor pago — confira na"
                " casa",
            )
        nosso = "GREEN" if estado == "WON" else "ANULADA"
        return (nosso, _milesimos(pago, "payout"), None)
    if not estado:
        return ("PENDENTE", None, "a casa não disse em que pé está esta aposta — confira na casa")
    return (
        "PENDENTE",
        None,
        f"a casa marcou esta aposta como {estado!r}, que eu ainda não sei ler — a minha amostra"
        " tinha só apostas liquidadas (LOST, WON e VOID). Confira e me diga o que ela foi",
    )


def _o_produto_das_pernas_fecha(bruto: dict[str, Any], total: float) -> str | None:
    # O total mora em `bets[].playedOdds` e as pernas em `couponRows[].playedOdds`, pontas
    # independentes: divergir é leitura errada, mas a aposta existe e retém em vez de sumir.
    pernas: list[float] = []
    for linha_do_cupom in bruto.get("couponRows") or []:
        if isinstance(linha_do_cupom, dict):
            perna = _milhar(linha_do_cupom.get("playedOdds"))
            if perna is None or perna <= 0:
                return None
            pernas.append(perna)
    if len(pernas) < 2:
        return None
    produto = 1.0
    for perna in pernas:
        produto *= perna
    # A folga acompanha o tamanho da odd: uma múltipla de 146 não se mede com a régua de uma de 2,00.
    if abs(produto - total) <= max(0.01, total * 0.0005):
        return None
    return (
        f"as odds não fecham: as {len(pernas)} pernas multiplicadas dão {produto:.3f} e a casa diz"
        f" que o total é {total:.3f} — confira na casa antes de contar esta aposta"
    )


def ler(bruto: object) -> Coletada:
    if not isinstance(bruto, dict):
        raise ColetaInvalidaError("a aposta não veio como objeto")

    # A identidade é o `couponRef`: o `couponExternalRef` veio com um pedaço colado (`…-noPba`) em
    # 2 dos 25 cupons medidos, e uma identidade que muda entre a captura em aberto e a liquidada
    # põe a mesma aposta duas vezes.
    identidade = str(bruto.get("couponRef") or bruto.get("couponExternalRef") or "").strip()
    if not identidade:
        raise ColetaInvalidaError("o cupom veio sem identificador (`couponRef`)")

    moeda = bruto.get("currency")
    if moeda and moeda != "BRL":
        raise ColetaInvalidaError(f"esta aposta está em {moeda}, e o planilhador conta em reais")

    # Um cupom de sistema carrega várias apostas sob uma identidade só, e dividi-la seria inventar
    # identidade: retém. Nos 25 cupons medidos havia exatamente uma.
    apostas = [a for a in (bruto.get("bets") or []) if isinstance(a, dict)]
    if not apostas:
        raise ColetaInvalidaError("o cupom veio sem nenhuma aposta dentro (`bets`)")

    aposta = apostas[0]
    motivo_lote: str | None = None
    if len(apostas) > 1:
        motivo_lote = (
            f"este cupom tem {len(apostas)} apostas dentro e eu ainda não sei separá-las — confira"
            " na casa"
        )

    stake = aposta.get("stake")
    if not isinstance(stake, (int, float)) or isinstance(stake, bool) or stake <= 0:
        raise ColetaInvalidaError("a aposta veio sem valor apostado")
    stake_centavos = _milesimos(stake, "stake")

    # A odd é a `playedOdds`, a que o usuário aceitou. O `betOdds` é a aplicada na liquidação:
    # veio 0 nas 21 perdidas e 1000 na anulada.
    odd = _milhar(aposta.get("playedOdds"))
    if odd is None or odd <= 0:
        raise ColetaInvalidaError("a aposta veio sem a odd total (`playedOdds`)")

    colocada_em = instante_iso(bruto.get("placedDate"), "placedDate")

    escolhas = _escolhas(bruto)
    if not escolhas:
        raise ColetaInvalidaError("o cupom veio sem nenhuma escolha dentro")

    estado, retorno, motivo = _resultado(aposta)
    motivo = motivo or motivo_lote or _o_produto_das_pernas_fecha(bruto, odd)

    observacao: str | None = None
    for escolha in bruto.get("outcomes") or []:
        if isinstance(escolha, dict):
            razao = (escolha.get("settledInfo") or {}).get("voidReason")
            if razao:
                observacao = f"a casa anulou: {razao}"
                break

    comeca = mais_cedo([
        instante_iso(jogo["eventStartDate"], "events[].eventStartDate")
        for jogo in (bruto.get("events") or [])
        if isinstance(jogo, dict) and jogo.get("eventStartDate")
    ])

    return Coletada(
        identidade=identidade,
        casa="kambi",
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
        observacao=observacao,
        motivo_retencao=motivo,
        bruto=bruto,
    )
