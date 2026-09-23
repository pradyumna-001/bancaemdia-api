import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from bancaemdia.domain.conferencias import Bilhete
from bancaemdia.domain.projecao import (
    CAMPOS_DA_CRIACAO as CAMPOS_DA_CRIACAO,
)
from bancaemdia.domain.projecao import (
    MOTIVO_APAGADA as MOTIVO_APAGADA,
)
from bancaemdia.domain.projecao import (
    projetar as projetar,
)
from bancaemdia.domain.vocabulario import CASAS

SEM_SELECAO_LIDA = "(sem seleção lida)"
BILHETE_NAO_LIDO = "(bilhete não lido — preencha à mão)"
TOLERANCIA_DA_ODD = 0.005
TOLERANCIA_DA_STAKE = 0.001
# Grafias do tipster e erros de leitura vistos em bilhetes com link; `rei do pitaco` é a mesma
# empresa do `pitaco.bet.br` por decisão do dono.
VARIANTES_DE_CASA: dict[str, str] = {
    "vupi bet": "Vupi",
    "vupid": "Vupi",
    "pitaco.bet": "Pitaco",
    "pitacobet": "Pitaco",
    "rei do pitaco": "Pitaco",
    "king panda bet": "KingPanda",
    "kingpanda bet": "KingPanda",
    "vaidbet": "VaideBet",
    "lottu bet": "Lottu",
    "7k bet": "7K Bet",
    "7k.bet": "7K Bet",
    "betesporte": "BetEsporte",
    "faz1 bet": "Faz1 Bet",
    "joga junto bet": "JogaJunto",
    "bet junto": "JogaJunto",
    "esportiva bet": "Esportiva Bet",
    "esportiva": "Esportiva Bet",
}


@dataclass(frozen=True)
class CupomLido:
    bilhete: Bilhete
    motivo: str | None = None
    grave: bool = False


@dataclass(frozen=True)
class LeituraRecebida:
    bilhete: Bilhete | None = None
    motivo: str | None = None
    grave: bool = False
    cupons: tuple[CupomLido, ...] = ()
    nao_e_aposta: bool = False


@dataclass(frozen=True)
class NovaAposta:
    chave: str
    ordem: int
    origem: str
    chat_id: int | None
    message_id: int | None
    bilhete: Bilhete | None
    casa: str | None
    data_aposta: str | None
    revisao_motivo: str | None
    revisao_grave: bool
    confianca: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventoNovo:
    tipo: str
    fonte: str
    payload: dict[str, Any]
    confianca: float | None = None


def chave_de_nome(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]", "", sem_acento)


def _indexar_casas() -> dict[str, str]:
    indice: dict[str, str] = {}
    for oficial, dominio in CASAS:
        indice.setdefault(chave_de_nome(dominio.split(".")[0]), oficial)
        indice.setdefault(chave_de_nome(oficial), oficial)
    for variante, oficial in VARIANTES_DE_CASA.items():
        indice[chave_de_nome(variante)] = oficial
    return indice


INDICE_DE_CASAS = _indexar_casas()


def casa_canonica(nome: str | None) -> str | None:
    """`None` quer dizer "não reconheço", não "não é casa".

    A régua é o domínio, nunca a semelhança do nome: `bet365`, `BetPix365` e `Jackpot365`
    se parecem e não são a mesma casa.
    """
    comparavel = chave_de_nome(nome or "")
    return INDICE_DE_CASAS.get(comparavel) if comparavel else None


def escolher_casa(bilhete: Bilhete | None) -> tuple[str | None, str | None]:
    """Casa lida que não é conhecida não vira casa: volta como aviso, ao lado da foto.

    Gravar `Rodri` ou `Mundo FIFA` como casa punha no relatório por casa um lucro que nunca
    foi daquela casa.
    """
    lida = bilhete.casa if bilhete else None
    oficial = casa_canonica(lida)
    if oficial:
        return oficial, None
    return None, lida


def mercado_principal(bilhete: Bilhete) -> str | None:
    for selecao in bilhete.selecoes:
        if selecao.mercado and selecao.mercado.strip() not in ("", "—"):
            return selecao.mercado.strip()
    return None


def descricao(bilhete: Bilhete) -> str:
    partes = [
        s.escolha + (f" ({s.mercado})" if s.mercado and s.mercado != "—" else "")
        for s in bilhete.selecoes
        if s.escolha
    ]
    return " + ".join(partes) if partes else SEM_SELECAO_LIDA


def chave_telegram(chat_id: int, message_id: int, ordem: int = 0) -> str:
    return f"t:{chat_id}:{message_id}:{ordem}"


def _juntar(*partes: str | None) -> str | None:
    presentes = [p for p in partes if p]
    return " · ".join(presentes) if presentes else None


def _aviso_de_casa(nao_reconhecida: str | None) -> str | None:
    if not nao_reconhecida:
        return None
    return (
        f"casa não reconhecida: li {nao_reconhecida!r}, que não é casa de apostas"
        " — preencha na planilha"
    )


def apostas_da_leitura(
    leitura: LeituraRecebida,
    *,
    chat_id: int,
    message_id: int,
    data: str | None = None,
    valor_unidade_centavos: int,
) -> list[NovaAposta]:
    def nova(
        ordem: int,
        bilhete: Bilhete | None,
        casa: str | None,
        motivo: str | None,
        grave: bool,
    ) -> NovaAposta:
        payload: dict[str, Any] = {
            "origem": "telegram",
            "data_aposta": data,
            "chat_id": chat_id,
            "message_id": message_id,
            "ordem_na_mensagem": ordem,
            "casa": casa,
            "tipster": None,
            "evento": bilhete.evento if bilhete else None,
            "descricao": descricao(bilhete) if bilhete else BILHETE_NAO_LIDO,
            "mercado_bruto": mercado_principal(bilhete) if bilhete else None,
            "tipo_aposta": bilhete.tipo.upper() if bilhete else "SIMPLES",
            "odd": bilhete.odd_total if bilhete else None,
            "odd_original": bilhete.odd_original if bilhete else None,
            "comeca_em": bilhete.quando if bilhete else None,
            "stake_unidades": 0.0,
            "valor_unidade_centavos": valor_unidade_centavos,
            "freebet": False,
            "selecionada": True,
            "revisao_motivo": motivo,
            "revisao_grave": grave,
        }
        return NovaAposta(
            chave=chave_telegram(chat_id, message_id, ordem),
            ordem=ordem,
            origem="telegram",
            chat_id=chat_id,
            message_id=message_id,
            bilhete=bilhete,
            casa=casa,
            data_aposta=data,
            revisao_motivo=motivo,
            revisao_grave=grave,
            confianca=bilhete.confianca if bilhete else 0.0,
            payload=payload,
        )

    if leitura.nao_e_aposta:
        return []

    uteis = [c for c in leitura.cupons if not c.bilhete.ilegivel]
    if len(uteis) > 1:
        # Cada cupom nasce com os dados DELE, nunca clones do primeiro. Sem stakes do texto
        # para parear pela ordem, todos nascem graves, fora do ROI até alguém olhar a foto.
        aviso = (
            f"a foto tem {len(uteis)} cupons e o texto 0 stake(s)"
            " — preencha a stake de cada cupom olhando a foto"
        )
        apostas = []
        for ordem, cupom in enumerate(uteis):
            casa, nao_reconhecida = escolher_casa(cupom.bilhete)
            motivo = _juntar(cupom.motivo, aviso, _aviso_de_casa(nao_reconhecida))
            apostas.append(nova(ordem, cupom.bilhete, casa, motivo, True))
        return apostas

    bilhete, motivo, grave = leitura.bilhete, leitura.motivo, leitura.grave
    if uteis:
        bilhete = uteis[0].bilhete
        if uteis[0].motivo:
            motivo = _juntar(motivo, uteis[0].motivo)
            grave = grave or uteis[0].grave
    elif bilhete is None and leitura.cupons:
        bilhete = leitura.cupons[0].bilhete

    if bilhete is not None and bilhete.ilegivel:
        return []
    # Sem leitura a aposta ainda existe, grave e para completar à mão: pular a mensagem fazia
    # a aposta sumir sem rastro. Sem motivo não houve tentativa de leitura, e aí não há aposta.
    if bilhete is None and not motivo:
        return []

    casa, nao_reconhecida = escolher_casa(bilhete)
    motivo = _juntar(motivo, _aviso_de_casa(nao_reconhecida))
    return [nova(0, bilhete, casa, motivo, grave)]


def eventos_da_releitura(
    nova: NovaAposta, atual: dict[str, Any], protegidos: set[str]
) -> list[EventoNovo]:
    """A aposta já existe: só vira evento o que mudou, e nunca por cima do que a pessoa corrigiu.

    Odd que mudou é fato do mundo (`export`); odd que faltava é a leitura que melhorou (`ia`).
    """
    eventos: list[EventoNovo] = []
    bilhete = nova.bilhete
    odd = bilhete.odd_total if bilhete else None
    if (
        "odd" not in protegidos
        and odd is not None
        and atual.get("odd") is not None
        and abs(odd - atual["odd"]) > TOLERANCIA_DA_ODD
    ):
        eventos.append(EventoNovo("ODD_ALTERADA", "export", {"de": atual["odd"], "para": odd}))

    # A data da mensagem entra uma vez, e só onde estava vazia: não é correção de data errada.
    if not atual.get("data_aposta") and nova.data_aposta and "data_aposta" not in protegidos:
        eventos.append(EventoNovo("CORRECAO_MANUAL", "export", {"data_aposta": nova.data_aposta}))

    if bilhete is None:
        return eventos

    novos = {
        "casa": nova.casa,
        "evento": bilhete.evento,
        "descricao": descricao(bilhete),
        "tipo_aposta": bilhete.tipo.upper(),
        "comeca_em": bilhete.quando,
        "mercado_bruto": mercado_principal(bilhete),
    }
    mudancas: dict[str, Any] = {
        campo: valor
        for campo, valor in novos.items()
        if valor
        and campo not in protegidos
        and str(valor) != str(atual.get(campo) or "")
        and valor != SEM_SELECAO_LIDA
    }
    if bilhete.odd_total is not None and atual.get("odd") is None and "odd" not in protegidos:
        mudancas["odd"] = bilhete.odd_total
    # A marca de revisão acompanha a leitura de hoje, a menos que a pessoa já tenha olhado o
    # print e resolvido: aí o programa não discute com ela.
    if (
        "odd" not in protegidos
        and "revisao_motivo" not in protegidos
        and (nova.revisao_motivo or "") != (atual.get("revisao_motivo") or "")
    ):
        mudancas["revisao_motivo"] = nova.revisao_motivo
        mudancas["revisao_grave"] = nova.revisao_grave
    if mudancas:
        eventos.append(EventoNovo("CORRECAO_MANUAL", "ia", mudancas, bilhete.confianca))
    return eventos
