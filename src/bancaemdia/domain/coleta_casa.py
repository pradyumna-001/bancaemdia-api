import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from bancaemdia.coleta.leitores import LEITORES
from bancaemdia.coleta.leitura import Coletada, ColetaInvalidaError
from bancaemdia.domain.financeiro import Estado
from bancaemdia.domain.materializar import EventoNovo, casa_canonica

FONTE = "casa"
ODD_MINIMA = 1.01
ODD_MAXIMA = 1000
# O nome da casa vira chave de tabela e vem de fora: com barra, espaço ou 300 caracteres criaria
# linhas que ninguém consegue procurar nem desfazer.
NOME_DE_CASA = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


class ApostaInvalidaError(ValueError):
    pass


@dataclass
class Resultado:
    # É a resposta que a extensão já conhece: o painel mostra `em_duvida` e `iguais_a_existentes`,
    # e o corte por data e o pareador que preenchem esses campos ainda não foram portados.
    novas_contando: int = 0
    iguais_a_existentes: int = 0
    em_duvida: int = 0
    atualizadas: int = 0
    ja_conhecidas: int = 0
    recusadas: list[dict[str, object]] = field(default_factory=list)
    sem_leitor: list[dict[str, object]] = field(default_factory=list)
    antes_do_inicio: int = 0

    def para_json(self) -> dict[str, object]:
        return {
            "antes_do_inicio": self.antes_do_inicio,
            "novas_contando": self.novas_contando,
            "iguais_a_existentes": self.iguais_a_existentes,
            "em_duvida": self.em_duvida,
            "atualizadas": self.atualizadas,
            "ja_conhecidas": self.ja_conhecidas,
            "recusadas": self.recusadas,
            "sem_leitor": self.sem_leitor,
        }


def casa_do_envio(casa: str) -> str:
    nome = (casa or "").strip().lower()
    if not NOME_DE_CASA.match(nome):
        raise ColetaInvalidaError(
            "o envio não disse de que casa é (ou o nome veio estranho) — a extensão manda o nome"
            " junto, então isto é envio quebrado, não casa desconhecida"
        )
    return nome


def chave_casa(casa: str, identidade: str) -> str:
    return f"c:{casa}:{identidade}"


def casa_da_coleta(nome_oficial: str) -> str | None:
    # O cru guarda a casa oficial da tabela `casas`; o leitor e a chave usam o nome do envio.
    return next((casa for casa in LEITORES if casa_canonica(casa) == nome_oficial), None)


def hash_do_conteudo(bruto: object) -> str:
    # Mesma identidade com o mesmo conteúdo é reenvio; com conteúdo diferente, a aposta liquidou.
    texto = json.dumps(bruto, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _valores(valor: object) -> Iterator[object]:
    if isinstance(valor, dict):
        for chave, dentro in valor.items():
            yield chave
            yield from _valores(dentro)
    elif isinstance(valor, list):
        for dentro in valor:
            yield from _valores(dentro)
    else:
        yield valor


def _guardavel(valor: object) -> bool:
    if isinstance(valor, float):
        return math.isfinite(valor)
    if isinstance(valor, str):
        try:
            valor.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return "\x00" not in valor
    return True


def conferir_guardavel(bruto: object) -> None:
    # O jsonb do PostgreSQL recusa o caractere nulo e o número infinito, e meio emoji não vira
    # UTF-8: sem esta recusa, uma aposta assim derrubava o envio inteiro, a cada reenvio.
    if not all(_guardavel(valor) for valor in _valores(bruto)):
        raise ColetaInvalidaError(
            "a aposta veio com um caractere ou número que não dá para guardar (caractere nulo,"
            " emoji partido ao meio ou número infinito) — só ela ficou de fora"
        )


def validar(coletada: Coletada, valor_unidade_centavos: int) -> None:
    problemas: list[str] = []
    if not coletada.casa or not coletada.casa.strip():
        problemas.append("falta a casa de apostas")
    if coletada.odd is None or coletada.odd < ODD_MINIMA:
        problemas.append("a odd tem de ser pelo menos 1.01")
    elif coletada.odd > ODD_MAXIMA:
        problemas.append(f"odd {coletada.odd:g} é alta demais para ser real")
    if coletada.stake_centavos <= 0:
        problemas.append("o valor apostado tem de ser positivo")
    if coletada.estado not in set(Estado):
        problemas.append(f"estado desconhecido: {coletada.estado!r}")
    # Cashout não tem fórmula: sem o valor que a casa pagou, o lucro seria inventado.
    if coletada.estado == Estado.CASHOUT and coletada.retorno_centavos is None:
        problemas.append("cashout precisa do valor que a casa pagou")
    if valor_unidade_centavos <= 0:
        problemas.append("o valor da unidade tem de ser positivo")
    if problemas:
        raise ApostaInvalidaError("; ".join(problemas))


def _evento_do_resultado(estado: str, retorno_centavos: int | None) -> EventoNovo:
    if estado == Estado.CASHOUT:
        return EventoNovo("CASHOUT_REGISTRADO", FONTE, {"retorno_centavos": retorno_centavos})
    return EventoNovo(
        "RESULTADO_REGISTRADO",
        FONTE,
        {"estado": estado, "comissao_centavos": 0, "retorno_centavos": retorno_centavos},
    )


def eventos_da_criacao(coletada: Coletada, valor_unidade_centavos: int) -> list[EventoNovo]:
    payload: dict[str, Any] = {
        "origem": "casa",
        "data_aposta": coletada.data_aposta,
        "casa": coletada.casa.strip(),
        "tipster": None,
        "evento": coletada.evento,
        "descricao": coletada.descricao,
        "mercado_bruto": coletada.mercado_bruto,
        "tipo_aposta": coletada.tipo,
        "odd": coletada.odd,
        "comeca_em": coletada.comeca_em,
        "stake_unidades": coletada.stake_centavos / valor_unidade_centavos,
        "valor_unidade_centavos": valor_unidade_centavos,
        "freebet": False,
        "ao_vivo": False,
        "comissao_centavos": 0,
        "selecionada": True,
        "midia_hash": None,
        "revisao_motivo": coletada.motivo_retencao,
        "revisao_grave": bool(coletada.motivo_retencao),
    }
    eventos = [EventoNovo("APOSTA_CRIADA", FONTE, payload, 1.0)]
    if coletada.estado == Estado.CASHOUT:
        eventos.append(_evento_do_resultado(coletada.estado, coletada.retorno_centavos))
    elif coletada.estado != Estado.PENDENTE:
        resultado: dict[str, Any] = {"estado": coletada.estado, "comissao_centavos": 0}
        # Quem sabe, manda: com o valor que a casa pagou, a projeção não refaz odd vezes stake.
        if coletada.retorno_centavos is not None:
            resultado["retorno_centavos"] = coletada.retorno_centavos
        eventos.append(EventoNovo("RESULTADO_REGISTRADO", FONTE, resultado))
    return eventos


def eventos_do_resultado(coletada: Coletada, atual: dict[str, Any]) -> list[EventoNovo]:
    eventos: list[EventoNovo] = []
    # O estado final segue a última captura, na ordem que for: a casa corrige resultado já
    # liquidado e reabre aposta, e congelar na primeira liquidação deixava o resultado velho.
    mudou = coletada.estado != atual.get("estado", Estado.PENDENTE) or (
        coletada.retorno_centavos is not None
        and coletada.retorno_centavos != atual.get("retorno_centavos")
    )
    if mudou:
        eventos.append(_evento_do_resultado(coletada.estado, coletada.retorno_centavos))
    # O aviso viaja com a liquidação: a aposta que liquida com motivo de retenção não pode entrar
    # contando na capa sem marca, e o motivo que ela já tem não é sobrescrito.
    if coletada.motivo_retencao and not atual.get("revisao_motivo"):
        eventos.append(evento_do_aviso(coletada.motivo_retencao))
    return eventos


def evento_do_aviso(motivo: str) -> EventoNovo:
    return EventoNovo("CORRECAO_MANUAL", FONTE, {"revisao_motivo": motivo, "revisao_grave": True})


def recado_sem_leitor(casa: str, quantas: int, leitores: Iterable[str]) -> str:
    quantas_texto = "1 aposta" if quantas == 1 else f"{quantas} apostas"
    return (
        f"guardei {quantas_texto} da {casa} e ainda não sei ler o formato dela — nada entrou nas"
        " suas contas. O que ficou guardado é a prova crua, e é dela que eu aprendo a ler essa"
        f" casa. As que eu leio hoje são: {', '.join(sorted(leitores))}"
    )


def recado_de_teto(quantas: int, teto: int | None) -> str | None:
    if not teto or quantas < teto:
        return None
    return (
        f"você já enviou {quantas} apostas hoje, e o teto por dia é {teto}. As que faltam entram"
        " amanhã — nada se perdeu, e o seu histórico na casa continua lá."
    )
