import json
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO
from urllib.parse import urlparse

from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.extracao.precos import USD_POR_BILHETE_REFERENCIA

NOME_ARQUIVO_JSON = "result.json"
EXTENSAO_COMPACTADA = ".zip"
EXTENSAO_JSON = ".json"

# Tetos do arquivo compactado, conferidos na lista do zip antes de ler qualquer byte: o export do
# dono tinha 97 mensagens e 77 fotos, e foto do Telegram já vem comprimida (razão perto de 1:1).
MAXIMO_DE_ARQUIVOS = 20_000
MAXIMO_DESCOMPACTADO = 200 * 1024 * 1024
MAXIMO_DO_JSON = 64 * 1024 * 1024
MAXIMO_POR_FOTO = 10 * 1024 * 1024
RAZAO_MAXIMA = 100
TAMANHO_COM_RAZAO = 1024 * 1024


class ExportInvalidoError(ValueError):
    pass


@dataclass(frozen=True)
class MensagemBruta:
    chat_id: int
    message_id: int
    tipo: str
    data: datetime
    autor: str | None
    texto: str
    links: list[str] = field(default_factory=list)
    editada_em: datetime | None = None
    responde_a: int | None = None
    caminho_foto: str | None = None
    largura: int | None = None
    altura: int | None = None
    encaminhada_de: str | None = None
    bruto: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def e_mensagem(self) -> bool:
        return self.tipo == "message"

    @property
    def tem_foto(self) -> bool:
        return self.caminho_foto is not None

    @property
    def foi_editada(self) -> bool:
        return self.editada_em is not None


def reconstruir_texto(campo: object) -> str:
    if campo is None:
        return ""
    if isinstance(campo, str):
        return campo
    if not isinstance(campo, list):
        return ""
    partes: list[str] = []
    # Mensagem com link ou negrito troca a string por uma lista de pedaços, no mesmo arquivo: quem
    # assume string sempre perde ~12% das mensagens deste canal (medido no projeto antigo).
    for pedaco in campo:
        if isinstance(pedaco, str):
            partes.append(pedaco)
        elif isinstance(pedaco, dict):
            partes.append(str(pedaco.get("text", "")))
    return "".join(partes)


def extrair_links(campo: object) -> list[str]:
    if not isinstance(campo, list):
        return []
    links: list[str] = []
    for pedaco in campo:
        if not isinstance(pedaco, dict):
            continue
        tipo = pedaco.get("type")
        if tipo == "link":
            links.append(str(pedaco.get("text", "")))
        elif tipo == "text_link":
            # Link escondido atrás de um texto ("clique aqui"): a URL real fica em `href`.
            links.append(str(pedaco.get("href", "")))
    return [link for link in links if link]


# Prestação de contas: o print do painel do mês tem foto, e foto é o sinal mais forte de bilhete.
# Custou R$ 1.355 de dinheiro fantasma no projeto antigo (a progressão virou odd e a contagem de
# apostas virou descrição). `[ \t]` e não `\s`: com `\s*` o `6` final de uma URL colava com o
# "Apostas" do aviso legal duas linhas abaixo e um bilhete real virava comentário (medido).
RE_PRESTACAO_DE_CONTAS = re.compile(
    r"\b\d+[ \t]+(?:apostas|bets|entradas)\b"
    r"|\bparcial\s+(?:mensal|semanal|di[áa]ri[ao]|do\s+m[êe]s|do\s+dia)\b"
    r"|\d[ \t]*%[ \t]*roi\b"
    r"|\broi[ \t]*:?[ \t]*[+-]?\d+[.,]?\d*[ \t]*%",
    re.IGNORECASE,
)

RE_URL = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
RE_ODD_ARROBA = re.compile(r"(?:@\s*|\bodds?\s*:?\s*)(\d+(?:[.,]\d+)?)", re.IGNORECASE)

# Domínios que aparecem em link e não são casa. `form` entrou porque uma aposta real trazia
# `form.jotform.com` como único link e virava uma "casa" chamada Form.
DOMINIOS_IGNORADOS = frozenset({
    "t",
    "telegram",
    "youtube",
    "youtu",
    "instagram",
    "wa",
    "chat",
    "docs",
    "forms",
    "form",
    "google",
    "twitter",
    "x",
})
PREFIXOS_DE_HOST = ("www.", "m.", "app.", "br.")
ODD_MINIMA = 1.0
ODD_MAXIMA = 1000.0


def e_prestacao_de_contas(texto: str) -> bool:
    return RE_PRESTACAO_DE_CONTAS.search(texto) is not None


def extrair_urls(texto: str) -> list[str]:
    return [url.rstrip(".,;)") for url in RE_URL.findall(texto)]


def marca_do_dominio(url: str) -> str | None:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    for prefixo in PREFIXOS_DE_HOST:
        if host.startswith(prefixo):
            host = host[len(prefixo) :]
    return host.split(".")[0] or None


def casas_por_url(texto: str) -> list[str]:
    casas: list[str] = []
    for url in extrair_urls(texto):
        marca = marca_do_dominio(url)
        if not marca or marca in DOMINIOS_IGNORADOS:
            continue
        # O domínio não mente, então esta é a conferência forte contra a leitura da imagem. Casa
        # fora do vocabulário entra pelo próprio domínio: descartar seria perder aposta de verdade.
        casas.append(casa_canonica(marca) or marca.capitalize())
    return list(dict.fromkeys(casas))


def para_decimal(texto: str) -> float | None:
    if not texto:
        return None
    limpo = texto.strip().replace(" ", "").replace(",", ".")
    # Stake e odd são números pequenos, sem separador de milhar: mais de um ponto é lixo ("1.2.3").
    if not limpo or limpo.count(".") > 1:
        return None
    try:
        return float(limpo)
    except ValueError:
        return None


def odds_citadas(texto: str) -> list[float]:
    odds: list[float] = []
    for achado in RE_ODD_ARROBA.finditer(texto):
        valor = para_decimal(achado.group(1))
        if valor and ODD_MINIMA < valor < ODD_MAXIMA:
            odds.append(valor)
    return odds


def _para_data(valor: object) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor))
    except ValueError:
        return None


def _conferir_zip(arquivos: Sequence[zipfile.ZipInfo]) -> None:
    if len(arquivos) > MAXIMO_DE_ARQUIVOS:
        raise ExportInvalidoError(
            f"este zip tem {len(arquivos)} arquivos, mais do que um export do Telegram traz —"
            " compacte só a pasta do export"
        )
    total = sum(info.file_size for info in arquivos)
    if total > MAXIMO_DESCOMPACTADO:
        raise ExportInvalidoError(
            "o conteúdo deste zip é grande demais para abrir de uma vez — exporte um período menor"
        )
    for info in arquivos:
        if info.flag_bits & 0x1:
            raise ExportInvalidoError("este zip está protegido por senha e não dá para abrir")
        # A foto do Telegram já vem comprimida: uma razão alta num arquivo grande é arquivo que
        # cresce ao abrir, não export.
        if (
            info.file_size > TAMANHO_COM_RAZAO
            and info.compress_size
            and info.file_size / info.compress_size > RAZAO_MAXIMA
        ):
            raise ExportInvalidoError("este zip não parece um export do Telegram")


class ExportTelegram:
    def __init__(self, arquivo: BinaryIO, nome_do_arquivo: str) -> None:
        self.nome_do_arquivo = nome_do_arquivo
        self._zip: zipfile.ZipFile | None = None
        self._prefixo = ""
        self._tamanhos: dict[str, int] = {}
        try:
            dados = self._carregar(arquivo, nome_do_arquivo)
        except BaseException:
            # A recusa acontece antes de o chamador ter o objeto: sem isto o zip aberto ficaria
            # para trás a cada arquivo recusado.
            self.fechar()
            raise
        if not isinstance(dados, dict) or "messages" not in dados:
            self.fechar()
            # O export da conta inteira traz `chats`, não `messages`: dizer só "falta messages"
            # mandaria a pessoa conferir o formato, que está certo — errado é o que ela exportou.
            if isinstance(dados, dict) and "chats" in dados:
                raise ExportInvalidoError(
                    "este é o export da conta inteira do Telegram. Exporte o histórico do chat das"
                    " apostas: no Telegram Desktop, menu do chat → Exportar histórico do chat."
                )
            raise ExportInvalidoError(
                f"{NOME_ARQUIVO_JSON} não tem a chave 'messages'. Confira se a exportação foi feita"
                " no formato JSON."
            )
        mensagens = dados["messages"]
        if not isinstance(mensagens, list):
            self.fechar()
            raise ExportInvalidoError(f"a chave 'messages' do {NOME_ARQUIVO_JSON} não é uma lista")
        self.nome = str(dados.get("name", "(sem nome)"))
        self.tipo = str(dados.get("type", "desconhecido"))
        try:
            self.chat_id = int(dados.get("id", 0))
        except (TypeError, ValueError) as sem_chat:
            self.fechar()
            raise ExportInvalidoError(
                "este arquivo não parece um export do Telegram: o chat não tem número"
            ) from sem_chat
        self._mensagens: list[Any] = mensagens

    def _carregar(self, arquivo: BinaryIO, nome_do_arquivo: str) -> Any:
        sufixo = nome_do_arquivo.lower()
        if sufixo.endswith(EXTENSAO_COMPACTADA):
            return self._carregar_do_zip(arquivo)
        if sufixo.endswith(EXTENSAO_JSON):
            return self._ler_json(arquivo.read(MAXIMO_DO_JSON + 1))
        raise ExportInvalidoError(
            f"mande o {EXTENSAO_COMPACTADA} da pasta que o Telegram Desktop gerou"
            f" (ou o {NOME_ARQUIVO_JSON} sozinho)"
        )

    def _carregar_do_zip(self, arquivo: BinaryIO) -> Any:
        try:
            self._zip = zipfile.ZipFile(arquivo)
            arquivos = self._zip.infolist()
        except zipfile.BadZipFile as quebrado:
            raise ExportInvalidoError(
                "não consegui abrir este zip — reenvie o arquivo que o Telegram Desktop gerou"
            ) from quebrado
        _conferir_zip(arquivos)
        self._tamanhos = {info.filename: info.file_size for info in arquivos}
        nome_interno = self._achar_json(arquivos)
        if self._tamanhos[nome_interno] > MAXIMO_DO_JSON:
            raise ExportInvalidoError(
                f"o {NOME_ARQUIVO_JSON} deste export é grande demais — exporte um período menor"
            )
        with self._zip.open(nome_interno) as interno:
            return self._ler_json(interno.read())

    def _ler_json(self, bruto: bytes) -> Any:
        if len(bruto) > MAXIMO_DO_JSON:
            raise ExportInvalidoError(
                f"o {NOME_ARQUIVO_JSON} deste export é grande demais — exporte um período menor"
            )
        try:
            # utf-8 explícito: no Windows o padrão ainda é cp1252, que quebra em acento e emoji.
            return json.loads(bruto.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as ilegivel:
            raise ExportInvalidoError(
                f"não consegui ler o {NOME_ARQUIVO_JSON} deste export"
            ) from ilegivel

    def _achar_json(self, arquivos: Sequence[zipfile.ZipInfo]) -> str:
        candidatos = [
            info.filename for info in arquivos if info.filename.endswith(NOME_ARQUIVO_JSON)
        ]
        if not candidatos:
            raise ExportInvalidoError(f"Não achei {NOME_ARQUIVO_JSON} dentro do zip.")
        # Mais de um export no mesmo zip: recusar, não escolher. O dono compactou a pasta
        # `Telegram Desktop` inteira, o código lia o export VELHO e a tela dizia que deu certo
        # (19/08/2026). Qual export vale é decisão da pessoa: podem ser chats diferentes.
        if len(candidatos) > 1:
            pastas = sorted(
                nome[: -len(NOME_ARQUIVO_JSON)].strip("/").split("/")[-1] or "(raiz)"
                for nome in candidatos
            )
            raise ExportInvalidoError(
                f"Achei {len(candidatos)} exports dentro deste zip: "
                + ", ".join(pastas)
                + ". Não dá para saber qual é o que você quer — e ler o errado traria as apostas de"
                " outro dia sem avisar. Compacte só a pasta do export que você quer (a"
                f" `{pastas[-1]}`, por exemplo), não a pasta `Telegram Desktop` inteira."
            )
        escolhido = candidatos[0]
        self._prefixo = escolhido[: -len(NOME_ARQUIVO_JSON)]
        return escolhido

    def mensagens(self, incluir_servico: bool = False) -> Iterator[MensagemBruta]:
        for cru in self._mensagens:
            if not isinstance(cru, dict):
                continue
            if not incluir_servico and cru.get("type") != "message":
                continue
            data = _para_data(cru.get("date"))
            # Sem data não dá para ordenar nem casar com a unidade vigente do dia.
            if data is None:
                continue
            try:
                message_id = int(cru.get("id", 0))
            except (TypeError, ValueError):
                # Mensagem sem número não casa com aposta nenhuma; o resto do export continua.
                continue
            texto = cru.get("text")
            caminho_foto = cru.get("photo")
            yield MensagemBruta(
                chat_id=self.chat_id,
                message_id=message_id,
                tipo=str(cru.get("type", "")),
                data=data,
                autor=cru.get("author") or cru.get("from"),
                texto=reconstruir_texto(texto),
                links=extrair_links(texto),
                editada_em=_para_data(cru.get("edited")),
                responde_a=cru.get("reply_to_message_id"),
                caminho_foto=caminho_foto if isinstance(caminho_foto, str) else None,
                largura=cru.get("width"),
                altura=cru.get("height"),
                encaminhada_de=cru.get("forwarded_from"),
                bruto=cru,
            )

    def _alvo(self, mensagem: MensagemBruta) -> str | None:
        if not mensagem.caminho_foto:
            return None
        alvo = self._prefixo + mensagem.caminho_foto
        return alvo if alvo in self._tamanhos else None

    def tamanho_da_foto(self, mensagem: MensagemBruta) -> int:
        alvo = self._alvo(mensagem)
        return 0 if alvo is None else self._tamanhos[alvo]

    def foto_existe(self, mensagem: MensagemBruta) -> bool:
        # Exportar sem marcar "fotos" mantém os caminhos no JSON, e o Telegram ainda troca o
        # caminho por um recado ("(File not included...)"): a régua é o arquivo estar no zip.
        return self._alvo(mensagem) is not None

    def bytes_da_foto(self, mensagem: MensagemBruta) -> bytes | None:
        alvo = self._alvo(mensagem)
        if alvo is None or self._zip is None:
            return None
        if self._tamanhos[alvo] > MAXIMO_POR_FOTO:
            return None
        # Vazio é o mesmo que ausente: uma foto de zero byte iria para a IA ler o nada.
        return self._zip.read(alvo) or None

    def __enter__(self) -> "ExportTelegram":
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()

    def fechar(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None


@dataclass(frozen=True)
class BilheteDoExport:
    mensagem: MensagemBruta
    tamanho: int
    ignorado: bool


@dataclass(frozen=True)
class Estimativa:
    total_mensagens: int
    bilhetes: int
    ignorados: int
    acima_do_teto: int
    custo_usd: float


def bilhetes_do_export(export: ExportTelegram) -> list[BilheteDoExport]:
    # A mesma lista serve para orçar e para ler: o que não tem foto no arquivo não custa nem vira
    # bilhete, e o print de prestação de contas do mês não paga leitura.
    return [
        BilheteDoExport(
            mensagem=mensagem,
            tamanho=export.tamanho_da_foto(mensagem),
            ignorado=e_prestacao_de_contas(mensagem.texto),
        )
        for mensagem in export.mensagens()
        if export.foto_existe(mensagem) and export.tamanho_da_foto(mensagem) > 0
    ]


def estimar(
    total_mensagens: int, bilhetes: Sequence[BilheteDoExport], restante_hoje: int | None = None
) -> Estimativa:
    a_ler = [bilhete for bilhete in bilhetes if not bilhete.ignorado]
    dentro = len(a_ler) if restante_hoje is None else min(len(a_ler), max(restante_hoje, 0))
    # O preço de referência é por FOTO lida, não por aposta: um print com três cupons custa uma
    # leitura só, e a foto que já está no cache sai de graça (a conta é o teto, não a fatura).
    return Estimativa(
        total_mensagens=total_mensagens,
        bilhetes=dentro,
        ignorados=len(bilhetes) - len(a_ler),
        acima_do_teto=len(a_ler) - dentro,
        custo_usd=dentro * USD_POR_BILHETE_REFERENCIA,
    )
