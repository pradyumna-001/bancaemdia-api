import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

ODD_MINIMA = 1.01
ODD_MAXIMA = 1000.0
ODD_MAXIMA_POR_PERNA = 50.0
ODD_MAXIMA_MULTIPLA = 500.0
CONFIANCA_MINIMA = 0.80
MEIA_CASA = 0.005
TOLERANCIA_DA_ODD_CITADA = 0.02
JANELA_DO_JOGO = range(-2, 31)

SEPARADORES_DE_CONFRONTO = r"\s+(?:x|v|vs\.?|versus|@)\s+|\s+[-\u2013\u2014]\s+"
RE_CONFRONTO = re.compile(SEPARADORES_DE_CONFRONTO, re.IGNORECASE)
RE_SO_NUMERO = re.compile(r"^[\d.,]+$")
RE_LIXO_NO_EVENTO = re.compile(
    r"R\s*\$|\bR\s*S\s*\d|\d{2}/\d{2}/\d{4}|\d{1,2}:\d{2}|》|>>|@\s*\d",
    re.IGNORECASE,
)
RE_MERCADO_NO_EVENTO = re.compile(
    r"retorn|aposta|maisde|menosde|assaltos?|rounds?|metodode|"
    r"handicap|escanteios?|cart[oõ]es|resultadofinal|totalde|desarmes?|"
    r"jogador|finaliza|assist|substituicao|"
    r"turbinada|superodd|goldenboost|supersub|limpartudo|escolhercasa|"
    r"criaraposta|dicasdeaposta|desafio|"
    r"vencedor|resultado|classificaca|classificatorio|chancedupla|"
    r"combinacaovencedora|top\dpilotos|melhorposicao|"
    r"temporegulamentar|terminaentre|"
    r"aovivo|especiaisdodia|especiais|longoprazo|supercombinada|"
    r"marcaou|d[aá]assist|darassist|amarcar|marca\d|todosganham",
    re.IGNORECASE,
)


class TipoBilhete(StrEnum):
    SIMPLES = "SIMPLES"
    MULTIPLA = "MULTIPLA"
    CRIAR_APOSTA = "CRIAR_APOSTA"
    SISTEMA = "SISTEMA"


class Origem(StrEnum):
    OCR = "OCR"
    IA = "IA"


class Forca(StrEnum):
    PROVA = "PROVA"
    ERRO = "ERRO"
    CONFIRMAR = "CONFIRMAR"
    TEXTO = "TEXTO"
    DUVIDA = "DUVIDA"
    FRACA = "FRACA"


class Veredito(StrEnum):
    APROVADO = "APROVADO"
    REVISAO = "REVISAO"
    ESCALONAR = "ESCALONAR"
    NAO_E_APOSTA = "NAO_E_APOSTA"


GRAVES: frozenset[Forca] = frozenset({Forca.PROVA, Forca.ERRO})
ESCALONAM: frozenset[Forca] = GRAVES | {Forca.TEXTO, Forca.CONFIRMAR}


@dataclass(frozen=True)
class Selecao:
    mercado: str
    escolha: str
    linha: float | None = None
    odd: float | None = None
    evento: str | None = None


@dataclass(frozen=True)
class Bilhete:
    casa: str | None = None
    tipo: TipoBilhete = TipoBilhete.SIMPLES
    evento: str | None = None
    selecoes: tuple[Selecao, ...] = ()
    odd_total: float | None = None
    odd_original: float | None = None
    quando: str | None = None
    ilegivel: bool = False
    confianca: float = 0.0

    @property
    def odds_das_selecoes(self) -> list[float]:
        return [s.odd for s in self.selecoes if s.odd is not None]

    @property
    def eventos_distintos(self) -> set[str]:
        nomes = {(s.evento or self.evento or "").strip().lower() for s in self.selecoes}
        return {n for n in nomes if n}

    @property
    def odds_devem_multiplicar(self) -> bool:
        if self.tipo in (TipoBilhete.CRIAR_APOSTA, TipoBilhete.SISTEMA):
            return False
        if len(self.selecoes) < 2:
            return False
        if len(self.odds_das_selecoes) != len(self.selecoes):
            return False
        return len(self.eventos_distintos) > 1


@dataclass(frozen=True)
class ConferenciaResultado:
    nome: str
    forca: Forca
    campo: str
    passou: bool = True
    mensagem: str = ""


@dataclass(frozen=True)
class Parecer:
    veredito: Veredito
    conferencias: tuple[ConferenciaResultado, ...]

    @property
    def falhas(self) -> list[ConferenciaResultado]:
        return [c for c in self.conferencias if not c.passou]

    @property
    def grave(self) -> bool:
        return any(c.forca in GRAVES for c in self.falhas)

    @property
    def motivo(self) -> str | None:
        if not self.falhas:
            return None
        return "; ".join(f"{c.nome}: {c.mensagem}" for c in self.falhas)


def teto_de_odd(quantidade_de_selecoes: int) -> float:
    pernas = max(1, quantidade_de_selecoes)
    if pernas == 1:
        return ODD_MAXIMA_POR_PERNA
    return max(ODD_MAXIMA_MULTIPLA, ODD_MAXIMA_POR_PERNA * pernas)


def conferir(
    bilhete: Bilhete,
    confianca_minima: float = CONFIANCA_MINIMA,
    casas_do_link: Iterable[str] = (),
    odds_do_texto: Iterable[float] = (),
    data_da_mensagem: datetime | None = None,
    origem: Origem | None = None,
) -> Parecer:
    conferencias = (
        _legibilidade(bilhete),
        _campos_essenciais(bilhete),
        _evento_plausivel(bilhete),
        _texto_aproveitavel(bilhete),
        _descricao_aproveitavel(bilhete),
        _faixa_das_odds(bilhete),
        _teto_de_odd(bilhete),
        _odd_nao_e_a_linha(bilhete, origem),
        _coerencia_das_odds(bilhete),
        _turbinada(bilhete),
        _confianca(bilhete, confianca_minima),
        _data_do_jogo(bilhete, data_da_mensagem),
        _casa_bate_com_link(bilhete, casas_do_link),
        _odd_bate_com_texto(bilhete, odds_do_texto),
    )
    if bilhete.ilegivel:
        return Parecer(Veredito.NAO_E_APOSTA, conferencias)

    forcas = {c.forca for c in conferencias if not c.passou}
    if forcas & ESCALONAM:
        veredito = Veredito.ESCALONAR
    elif forcas:
        veredito = Veredito.REVISAO
    else:
        veredito = Veredito.APROVADO
    return Parecer(veredito, conferencias)


def corrigir_ano(bilhete: Bilhete, data_da_mensagem: datetime) -> str | None:
    if bilhete.quando is None:
        return None
    jogo = _data_lida(bilhete.quando)
    if jogo is None:
        return None
    corrigida = _perto_da_mensagem(jogo, data_da_mensagem.replace(tzinfo=None))
    return (corrigida or jogo).isoformat(timespec="minutes")


def _legibilidade(bilhete: Bilhete) -> ConferenciaResultado:
    return ConferenciaResultado(
        nome="legibilidade",
        forca=Forca.ERRO,
        campo="ilegivel",
        passou=not bilhete.ilegivel,
        mensagem="a IA marcou a imagem como ilegível" if bilhete.ilegivel else "",
    )


def _campos_essenciais(bilhete: Bilhete) -> ConferenciaResultado:
    faltando = []
    if not bilhete.selecoes:
        faltando.append("nenhuma seleção")
    if bilhete.odd_total is None:
        faltando.append("odd total")
    return ConferenciaResultado(
        nome="campos essenciais",
        forca=Forca.ERRO,
        campo="selecoes" if not bilhete.selecoes else "odd_total",
        passou=not faltando,
        mensagem=", ".join(faltando),
    )


def _nao_parece_confronto(nome: str) -> bool:
    return bool(
        RE_LIXO_NO_EVENTO.search(nome)
        or RE_MERCADO_NO_EVENTO.search(re.sub(r"\s+", "", nome))
        or len(nome.strip()) < 4
    )


def _evento_plausivel(bilhete: Bilhete) -> ConferenciaResultado:
    nomes = [bilhete.evento, *(s.evento for s in bilhete.selecoes)]
    suspeitos = [n for n in nomes if n and _nao_parece_confronto(n)]
    return ConferenciaResultado(
        nome="evento plausível",
        forca=Forca.ERRO,
        campo="evento",
        passou=not suspeitos,
        mensagem=f"não parece confronto: {suspeitos[0][:50]!r}" if suspeitos else "",
    )


def _texto_aproveitavel(bilhete: Bilhete) -> ConferenciaResultado:
    evento = (bilhete.evento or "").strip() or next(
        (s.evento.strip() for s in bilhete.selecoes if s.evento and s.evento.strip()), ""
    )
    if not evento:
        mensagem = "não dá para saber qual jogo é: nenhum confronto foi lido"
    elif not RE_CONFRONTO.search(evento):
        mensagem = f"só um lado do confronto foi lido: {evento[:40]!r} — falta saber contra quem"
    else:
        mensagem = ""
    return ConferenciaResultado(
        nome="texto aproveitável",
        forca=Forca.TEXTO,
        campo="evento",
        passou=not mensagem,
        mensagem=mensagem,
    )


def _diz_alguma_coisa(selecao: Selecao) -> bool:
    mercado = selecao.mercado.strip()
    if mercado and mercado != "—":
        return True
    escolha = selecao.escolha.strip()
    return bool(escolha) and not RE_SO_NUMERO.match(escolha)


def _descricao_aproveitavel(bilhete: Bilhete) -> ConferenciaResultado:
    aproveitaveis = [s for s in bilhete.selecoes if _diz_alguma_coisa(s)]
    passou = not bilhete.selecoes or bool(aproveitaveis)
    soltas = ", ".join(s.escolha.strip() for s in bilhete.selecoes)
    return ConferenciaResultado(
        nome="descrição aproveitável",
        forca=Forca.TEXTO,
        campo="selecoes",
        passou=passou,
        mensagem=(
            f"não dá para saber o que foi apostado: a seleção é só um número ({soltas!r})"
            if not passou
            else ""
        ),
    )


def _faixa_das_odds(bilhete: Bilhete) -> ConferenciaResultado:
    todas = bilhete.odds_das_selecoes
    if bilhete.odd_total is not None:
        todas = [*todas, bilhete.odd_total]
    fora = [o for o in todas if not ODD_MINIMA <= o <= ODD_MAXIMA]
    return ConferenciaResultado(
        nome="faixa das odds",
        forca=Forca.ERRO,
        campo="odd_total",
        passou=not fora,
        mensagem=f"odd fora da faixa plausível: {fora}" if fora else "",
    )


def _teto_de_odd(bilhete: Bilhete) -> ConferenciaResultado:
    pernas = len(bilhete.selecoes)
    teto = teto_de_odd(pernas)
    passou = bilhete.odd_total is None or not pernas or bilhete.odd_total <= teto
    quantas = "numa perna só" if pernas == 1 else f"em {pernas} pernas"
    return ConferenciaResultado(
        nome="teto de odd",
        forca=Forca.CONFIRMAR,
        campo="odd_total",
        passou=passou,
        mensagem=(
            f"odd {bilhete.odd_total:g} {quantas} é fora do comum (acima de {teto:g})"
            " — se o print diz isso mesmo, é só confirmar"
            if not passou
            else ""
        ),
    )


def _odd_nao_e_a_linha(bilhete: Bilhete, origem: Origem | None) -> ConferenciaResultado:
    odd_total = bilhete.odd_total
    linhas = {s.linha for s in bilhete.selecoes if s.linha is not None}
    iguais = [] if odd_total is None else [x for x in linhas if abs(x - odd_total) < MEIA_CASA]
    leu_a_ia = origem == Origem.IA
    if not iguais:
        mensagem = ""
    elif leu_a_ia:
        mensagem = (
            f"odd {odd_total:g} é igual à linha do mercado ({iguais[0]:g})"
            " — coincidência comum em aposta de jogador; se o print mostra essa odd,"
            " é só confirmar"
        )
    else:
        mensagem = (
            f"odd {odd_total:g} é igual à linha do mercado ({iguais[0]:g})"
            " — provável troca de um pelo outro"
        )
    return ConferenciaResultado(
        nome="odd ≠ linha",
        forca=Forca.CONFIRMAR if leu_a_ia else Forca.ERRO,
        campo="odd_total",
        passou=not iguais,
        mensagem=mensagem,
    )


def _coerencia_das_odds(bilhete: Bilhete) -> ConferenciaResultado:
    odds = bilhete.odds_das_selecoes
    odd_total = bilhete.odd_total
    passou = True
    if not odds or odd_total is None:
        mensagem = "sem dados para conferir"
    elif len(odds) > 1 and not bilhete.odds_devem_multiplicar:
        mensagem = f"não se aplica a {bilhete.tipo}"
    elif not odd_total:
        mensagem = "sem dados para conferir"
    else:
        produto = math.prod(odds)
        tolerancia = sum(MEIA_CASA / o for o in odds if o) + MEIA_CASA / odd_total
        diferenca = abs(produto - odd_total) / odd_total
        passou = diferenca <= tolerancia
        if passou:
            mensagem = ""
        elif len(odds) == 1:
            mensagem = f"simples: seleção {odds[0]:g} ≠ total {odd_total:g}"
        else:
            mensagem = (
                f"múltipla: {' x '.join(f'{o:g}' for o in odds)} = {produto:.4f}, "
                f"mas o bilhete diz {odd_total:g} "
                f"(diferença {diferenca:.1%}, tolerado {tolerancia:.1%})"
            )
    return ConferenciaResultado(
        nome="coerência das odds",
        forca=Forca.PROVA,
        campo="odd_total",
        passou=passou,
        mensagem=mensagem,
    )


def _turbinada(bilhete: Bilhete) -> ConferenciaResultado:
    riscada, valendo = bilhete.odd_original, bilhete.odd_total
    passou = riscada is None or valendo is None or riscada < valendo
    return ConferenciaResultado(
        nome="odd turbinada",
        forca=Forca.ERRO,
        campo="odd_original",
        passou=passou,
        mensagem=(
            f"a riscada ({riscada:g}) deveria ser MENOR que a valendo ({valendo:g})"
            if not passou
            else ""
        ),
    )


def _confianca(bilhete: Bilhete, minima: float) -> ConferenciaResultado:
    passou = bilhete.confianca >= minima
    return ConferenciaResultado(
        nome="confiança",
        forca=Forca.DUVIDA,
        campo="confianca",
        passou=passou,
        mensagem=f"{bilhete.confianca:.2f} abaixo do mínimo {minima:.2f}" if not passou else "",
    )


def _data_lida(quando: str) -> datetime | None:
    try:
        return datetime.fromisoformat(quando).replace(tzinfo=None)
    except ValueError:
        return None


def _perto_da_mensagem(jogo: datetime, referencia: datetime) -> datetime | None:
    if (jogo - referencia).days in JANELA_DO_JOGO:
        return jogo
    for ano in (referencia.year, referencia.year + 1):
        try:
            corrigida = jogo.replace(year=ano)
        except ValueError:
            continue
        if (corrigida - referencia).days in JANELA_DO_JOGO:
            return corrigida
    return None


def _data_do_jogo(bilhete: Bilhete, data_da_mensagem: datetime | None) -> ConferenciaResultado:
    passou, mensagem = True, ""
    if bilhete.quando is not None and data_da_mensagem is not None:
        jogo = _data_lida(bilhete.quando)
        referencia = data_da_mensagem.replace(tzinfo=None)
        if jogo is None:
            passou, mensagem = False, f"data em formato inválido: {bilhete.quando!r}"
        else:
            corrigida = _perto_da_mensagem(jogo, referencia)
            if corrigida is None:
                dias = (jogo - referencia).days
                passou = False
                mensagem = f"jogo em {jogo:%d/%m/%Y} está a {dias} dias da mensagem"
            elif corrigida != jogo:
                mensagem = f"ano ausente no bilhete; assumido {corrigida.year}"
    return ConferenciaResultado(
        nome="data do jogo",
        forca=Forca.ERRO,
        campo="quando",
        passou=passou,
        mensagem=mensagem,
    )


def _casa_bate_com_link(bilhete: Bilhete, casas_do_link: Iterable[str]) -> ConferenciaResultado:
    casas = list(casas_do_link)
    da_imagem = (bilhete.casa or "").strip().lower()
    dos_links = [c.strip().lower() for c in casas]
    passou = (
        not casas
        or bilhete.casa is None
        or any(da_imagem in c or c in da_imagem for c in dos_links)
    )
    return ConferenciaResultado(
        nome="casa x link",
        forca=Forca.FRACA,
        campo="casa",
        passou=passou,
        mensagem=(
            f"bilhete parece {bilhete.casa}, mas o link é de {', '.join(casas)}"
            if not passou
            else ""
        ),
    )


def _odd_bate_com_texto(bilhete: Bilhete, odds_do_texto: Iterable[float]) -> ConferenciaResultado:
    citadas = list(odds_do_texto)
    total = bilhete.odd_total or 0.0
    passou = (
        not citadas
        or not total
        or any(abs(o - total) / total <= TOLERANCIA_DA_ODD_CITADA for o in citadas)
    )
    return ConferenciaResultado(
        nome="odd x texto",
        forca=Forca.FRACA,
        campo="odd_total",
        passou=passou,
        mensagem=(
            f"bilhete diz {total:g}, texto cita {', '.join(f'{o:g}' for o in citadas)}"
            if not passou
            else ""
        ),
    )
