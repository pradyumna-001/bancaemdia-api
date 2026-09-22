import argparse
import asyncio
import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import structlog
from kombu.exceptions import OperationalError
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.coleta.leitores import LEITORES
from bancaemdia.coleta.leitura import ColetaInvalidaError
from bancaemdia.domain.coleta_casa import casa_da_coleta, casa_do_envio
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.registros import ApostasPorOrigem
from bancaemdia.domain.upload import casas_por_url, odds_citadas
from bancaemdia.extracao.cliente import VERSAO_PROMPT
from bancaemdia.extracao.precos import ORIGEM_DA_REFERENCIA, USD_POR_BILHETE_REFERENCIA, em_reais
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.workers.extraction import cadeia_do_bilhete
from bancaemdia.workers.materialization import (
    FUSO_DO_BRASIL,
    _set_current_user,
    get_engine,
    materializar_coletas_task,
)

ORIGEM_RELIDA = "telegram"
ESCOPO_REVISAO = "revisao"
ESCOPO_TUDO = "tudo"
PAGINA = 1000
LOTE = 100
SEM_FOTOS = "custo estimado antes da fila; fotos ausentes são registradas e puladas"
SEM_SIM = "nada foi enviado — rode de novo com --sim para reprocessar"
NADA_GUARDADO = (
    "não havia nada guardado desta casa: o histórico dela nunca foi aberto com a extensão ligada"
)
NADA_LEGIVEL = (
    "o cru guardado desta casa não se lê com o leitor de hoje — continua guardado, e nada foi"
    " perdido"
)
CONFIRA = (
    "quando o trabalhador terminar, confira o VALOR, a ODD e o RESULTADO contra a tela da casa. Um"
    " número plausível e errado por 100 vezes não é pego por teste nenhum. A aposta que o"
    " planilhador não aceita continua guardada e aparece como tarefa falha, na fila dead_letter"
)


class VersaoDoPromptError(ValueError):
    pass


class UsuarioNaoEncontradoError(ValueError):
    pass


class ColetaNaoEncontradaError(ValueError):
    pass


class FilaForaDoArError(RuntimeError):
    pass


@dataclass
class Plano:
    usuarios: int = 0
    apostas: int = 0
    bilhetes: int = 0
    fora_do_escopo: int = 0
    sem_releitura: dict[str, int] = field(default_factory=dict)
    midias_ausentes: int = 0
    enfileiradas: int = 0

    def somar(self, contagens: Sequence[ApostasPorOrigem], escopo: str) -> None:
        self.usuarios += 1
        for contagem in contagens:
            if contagem.origem != ORIGEM_RELIDA:
                self.sem_releitura[contagem.origem] = (
                    self.sem_releitura.get(contagem.origem, 0) + contagem.apostas
                )
                continue
            # Reler tudo custou R$ 18,85 com 84% já certos; só as falhas, R$ 2,92 (29/07/2026).
            if escopo == ESCOPO_TUDO:
                apostas, bilhetes = contagem.apostas, contagem.mensagens
            else:
                apostas, bilhetes = contagem.apostas_em_revisao, contagem.mensagens_em_revisao
            self.apostas += apostas
            self.bilhetes += bilhetes
            self.fora_do_escopo += contagem.apostas - apostas

    @property
    def custo_usd(self) -> float:
        # O preço é por bilhete, não por aposta: a foto com três cupons é lida uma vez só.
        return self.bilhetes * USD_POR_BILHETE_REFERENCIA


@dataclass
class Capturas:
    guardadas: int = 0
    ilegiveis: int = 0
    por_aposta: dict[str, list[tuple[datetime, int]]] = field(default_factory=dict)

    def historicos(self) -> list[list[int]]:
        return sorted(
            (
                [coleta_id for _, coleta_id in sorted(capturas)]
                for capturas in self.por_aposta.values()
            ),
            key=min,
        )


@dataclass
class Reprocesso:
    casa: str
    guardadas: int = 0
    ilegiveis: int = 0
    apostas: list[list[int]] = field(default_factory=list)
    enfileiradas: int = 0
    pedida: int | None = None


def conferir_versao(versao_prompt: str | None) -> None:
    # A leitura só roda o prompt publicado: aceitar outra versão faria o custo e o log falarem de um
    # prompt que não vai rodar.
    if versao_prompt is not None and versao_prompt != VERSAO_PROMPT:
        raise VersaoDoPromptError(
            f"a versão {versao_prompt!r} não é a do prompt publicado ({VERSAO_PROMPT}) — publique o"
            " prompt novo antes de pedir a releitura com ele"
        )


async def _conferir_usuario(engine: AsyncEngine, usuario_id: int) -> None:
    async with AsyncSession(engine) as session:
        usuario = await UsuarioRepo().get_by_id(session, usuario_id)
    if usuario is None:
        raise UsuarioNaoEncontradoError(f"o usuário {usuario_id} não existe")


async def _planejar(
    engine: AsyncEngine,
    usuario_id: int,
    desde: datetime | None,
    ate: datetime | None,
    escopo: str,
    plano: Plano,
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        await _set_current_user(session, usuario_id)
        contagens = await ApostaRepo().count_by_origem(session, usuario_id, desde, ate)
    plano.somar(contagens, escopo)


async def reprocessar_usuario(
    engine: AsyncEngine,
    usuario_id: int,
    desde: datetime | None = None,
    ate: datetime | None = None,
    versao_prompt: str | None = None,
    escopo: str = ESCOPO_REVISAO,
    *,
    sim: bool = False,
    chunk_size: int = PAGINA,
) -> Plano:
    conferir_versao(versao_prompt)
    await _conferir_usuario(engine, usuario_id)
    if sim:
        return await reler_todas(
            engine,
            versao_prompt or VERSAO_PROMPT,
            escopo,
            chunk_size=chunk_size,
            sim=True,
            usuario_id=usuario_id,
            desde=desde,
            ate=ate,
        )
    plano = Plano()
    await _planejar(engine, usuario_id, desde, ate, escopo, plano)
    return plano


async def reler_todas(
    engine: AsyncEngine,
    versao_prompt: str,
    escopo: str = ESCOPO_REVISAO,
    *,
    chunk_size: int = PAGINA,
    sim: bool = False,
    usuario_id: int | None = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
) -> Plano:
    conferir_versao(versao_prompt)
    if chunk_size < 1:
        raise ValueError("chunk_size deve ser positivo")
    plano = Plano()
    depois_de = 0
    limites: list[tuple[int, int]] = []
    while True:
        # Não há papel que atravesse a RLS: a lista de usuários é aberta, e cada um é contado com a
        # própria identidade. Página pelo id, porque OFFSET pula linhas quando alguém grava no meio.
        if usuario_id is None:
            async with AsyncSession(engine) as session:
                ids = await UsuarioRepo().list_active_ids(session, depois_de, PAGINA)
        else:
            ids = [usuario_id]
        for atual_id in ids:
            await _planejar(engine, atual_id, desde, ate, escopo, plano)
            async with AsyncSession(engine) as session, session.begin():
                await _set_current_user(session, atual_id)
                ultimo = await session.scalar(
                    select(func.max(models.UploadBilhete.id)).where(
                        models.UploadBilhete.usuario_id == atual_id
                    )
                )
            if ultimo is not None:
                limites.append((atual_id, int(ultimo)))
            if plano.usuarios % LOTE == 0:
                structlog.get_logger().info(
                    "reler_todas_progresso", usuarios=plano.usuarios, bilhetes=plano.bilhetes
                )
        if usuario_id is not None or len(ids) < PAGINA:
            break
        depois_de = ids[-1]

    # Count actual media candidates, bounded by the high-water mark, before publishing anything.
    candidatos = 0
    for usuario_id, ultimo in limites:
        depois: tuple[int, int] | None = None
        while True:
            async with AsyncSession(engine) as session, session.begin():
                await _set_current_user(session, usuario_id)
                pagina = await _pagina_midias(
                    session, usuario_id, ultimo, depois, escopo, chunk_size, desde, ate
                )
                for bilhete in pagina:
                    if bilhete.midia_hash is None:
                        plano.midias_ausentes += 1
                    else:
                        candidatos += 1
            if len(pagina) < chunk_size:
                break
            depois = (pagina[-1].chat_id, pagina[-1].message_id)
    plano.bilhetes = candidatos
    structlog.get_logger().info(
        "releitura_custo_antes_da_fila",
        versao_prompt=versao_prompt,
        bilhetes=candidatos,
        midias_sem_hash=plano.midias_ausentes,
        custo_estimado_usd=round(plano.custo_usd, 2),
        custo_estimado_brl=round(em_reais(plano.custo_usd), 2),
    )
    if not sim:
        return plano

    for usuario_id, ultimo in limites:
        depois = None
        while True:
            async with AsyncSession(engine) as session, session.begin():
                await _set_current_user(session, usuario_id)
                pagina = await _pagina_midias(
                    session, usuario_id, ultimo, depois, escopo, chunk_size, desde, ate
                )
            for bilhete in pagina:
                if bilhete.midia_hash is None:
                    continue
                async with AsyncSession(engine) as session, session.begin():
                    await _set_current_user(session, usuario_id)
                    mensagem = (
                        await session.execute(
                            select(models.Mensagem).where(
                                models.Mensagem.chat_id == bilhete.chat_id,
                                models.Mensagem.message_id == bilhete.message_id,
                            )
                        )
                    ).scalar_one_or_none()
                    dados = await MidiaArquivoRepo().get_by_hash(session, bilhete.midia_hash)
                    midia = (
                        await session.execute(
                            select(models.Midia).where(models.Midia.hash == bilhete.midia_hash)
                        )
                    ).scalar_one_or_none()
                if mensagem is None or dados is None or midia is None:
                    plano.midias_ausentes += 1
                    structlog.get_logger().warning(
                        "releitura_midia_ausente",
                        usuario_id=usuario_id,
                        chat_id=bilhete.chat_id,
                        message_id=bilhete.message_id,
                        midia_hash=bilhete.midia_hash,
                    )
                    continue
                texto = mensagem.texto
                try:
                    cadeia_do_bilhete(
                        usuario_id,
                        base64.b64encode(dados).decode("ascii"),
                        nome_do_arquivo=f"{bilhete.midia_hash}.{midia.tipo.rsplit('/', 1)[-1]}",
                        legenda=texto,
                        postada_em=mensagem.data
                        .astimezone(FUSO_DO_BRASIL)
                        .replace(tzinfo=None)
                        .isoformat(),
                        casas_do_link=casas_por_url(texto),
                        odds_do_texto=odds_citadas(texto),
                        chat_id=bilhete.chat_id,
                        message_id=bilhete.message_id,
                        midia_hash=bilhete.midia_hash,
                        versao_prompt=versao_prompt,
                    ).apply_async()
                except OperationalError as erro:
                    raise FilaForaDoArError(
                        f"fila caiu após {plano.enfileiradas} bilhetes; retome com --sim"
                    ) from erro
                plano.enfileiradas += 1
                if plano.enfileiradas % LOTE == 0:
                    structlog.get_logger().info(
                        "releitura_progresso",
                        enfileiradas=plano.enfileiradas,
                        estimadas=candidatos,
                    )
            if len(pagina) < chunk_size:
                break
            depois = (pagina[-1].chat_id, pagina[-1].message_id)
    return plano


async def _pagina_midias(
    session: AsyncSession,
    usuario_id: int,
    ultimo: int,
    depois: tuple[int, int] | None,
    escopo: str,
    tamanho: int,
    desde: datetime | None = None,
    ate: datetime | None = None,
) -> list[models.UploadBilhete]:
    # The tenant-owned upload row proves access to the globally deduplicated message/media.
    agrupados = (
        select(
            models.UploadBilhete.chat_id.label("chat_id"),
            models.UploadBilhete.message_id.label("message_id"),
            func.coalesce(
                func.max(models.UploadBilhete.id).filter(
                    models.UploadBilhete.midia_hash.is_not(None)
                ),
                func.max(models.UploadBilhete.id),
            ).label("ultimo_id"),
        )
        .where(
            models.UploadBilhete.usuario_id == usuario_id,
            models.UploadBilhete.id <= ultimo,
        )
        .group_by(models.UploadBilhete.chat_id, models.UploadBilhete.message_id)
        .subquery()
    )
    stmt = select(models.UploadBilhete).join(
        agrupados, models.UploadBilhete.id == agrupados.c.ultimo_id
    )
    if depois is not None:
        stmt = stmt.where(tuple_(agrupados.c.chat_id, agrupados.c.message_id) > depois)
    if escopo == ESCOPO_REVISAO or desde is not None or ate is not None:
        aposta = select(models.Aposta.id).where(
            models.Aposta.usuario_id == usuario_id,
            models.Aposta.chat_id == agrupados.c.chat_id,
            models.Aposta.message_id == agrupados.c.message_id,
        )
        if escopo == ESCOPO_REVISAO:
            aposta = aposta.where(models.Aposta.revisao_grave.is_(True))
        if desde is not None:
            aposta = aposta.where(models.Aposta.data_aposta >= desde)
        if ate is not None:
            aposta = aposta.where(models.Aposta.data_aposta < ate)
        stmt = stmt.where(aposta.exists())
    return list(
        (
            await session.execute(
                stmt.order_by(agrupados.c.chat_id, agrupados.c.message_id).limit(tamanho)
            )
        ).scalars()
    )


def _enfileirar(usuario_id: int, coleta_ids: list[int]) -> None:
    materializar_coletas_task.apply_async(
        kwargs={"usuario_id": usuario_id, "coleta_ids": coleta_ids}
    )


def _sem_leitor(casa: str) -> ColetaInvalidaError:
    return ColetaInvalidaError(
        f"ainda não sei ler o histórico da {casa} — o cru continua guardado, esperando o leitor."
        " Nada foi perdido"
    )


async def _capturas(engine: AsyncEngine, usuario_id: int, casa: str, casa_id: int) -> Capturas:
    ler = LEITORES[casa]
    capturas = Capturas()
    depois_de = 0
    while True:
        async with AsyncSession(engine) as session, session.begin():
            await _set_current_user(session, usuario_id)
            linhas = await ColetaCasaRepo().list_by_casa(
                session, usuario_id, casa_id, depois_de, PAGINA
            )
        for linha in linhas:
            capturas.guardadas += 1
            try:
                identidade = ler(linha.bruto_json).identidade
            except ColetaInvalidaError:
                capturas.ilegiveis += 1
                continue
            # Sem leitor, cada captura diferente da mesma aposta vira uma linha. Elas vão juntas numa
            # tarefa só: soltas, podiam rodar fora de ordem e a velha desfaria o resultado da nova.
            capturas.por_aposta.setdefault(identidade, []).append((linha.recebido_em, linha.id))
        if len(linhas) < PAGINA:
            return capturas
        depois_de = linhas[-1].id


def _enfileirar_todas(usuario_id: int, reprocesso: Reprocesso) -> None:
    for coleta_ids in reprocesso.apostas:
        try:
            _enfileirar(usuario_id, coleta_ids)
        except OperationalError as erro:
            raise FilaForaDoArError(
                f"a fila caiu depois de {reprocesso.enfileiradas} de {len(reprocesso.apostas)}"
                " apostas — as que entraram rodam, e rodar de novo com --sim termina o serviço sem"
                " duplicar nada"
            ) from erro
        reprocesso.enfileiradas += 1
        if reprocesso.enfileiradas % LOTE == 0:
            structlog.get_logger().info(
                "reprocessar_coleta_progresso",
                casa=reprocesso.casa,
                enfileiradas=reprocesso.enfileiradas,
                de=len(reprocesso.apostas),
            )


async def reprocessar_coleta(
    engine: AsyncEngine, usuario_id: int, coleta_id: int, *, sim: bool = False
) -> Reprocesso:
    await _conferir_usuario(engine, usuario_id)
    async with AsyncSession(engine) as session, session.begin():
        await _set_current_user(session, usuario_id)
        coleta = await ColetaCasaRepo().get_by_id(session, usuario_id, coleta_id)
        nome = None if coleta is None else await CasaRepo().get_nome_by_id(session, coleta.casa_id)
    if coleta is None or nome is None:
        raise ColetaNaoEncontradaError(f"não achei a coleta {coleta_id} deste usuário")
    casa = casa_da_coleta(nome)
    if casa is None:
        raise _sem_leitor(nome)
    identidade = LEITORES[casa](coleta.bruto_json).identidade
    capturas = await _capturas(engine, usuario_id, casa, coleta.casa_id)
    historico = [id_ for _, id_ in sorted(capturas.por_aposta[identidade])]
    reprocesso = Reprocesso(casa, len(historico), 0, [historico], pedida=coleta_id)
    # Mostrar antes de fazer é a regra da casa para tudo que mexe em dinheiro: sem --sim, nada sai.
    if sim:
        _enfileirar_todas(usuario_id, reprocesso)
    return reprocesso


async def reprocessar_casa(
    engine: AsyncEngine, usuario_id: int, casa: str, *, sim: bool = False
) -> Reprocesso:
    casa = casa_do_envio(casa)
    nome = casa_canonica(casa)
    if casa not in LEITORES or nome is None:
        raise _sem_leitor(casa)
    await _conferir_usuario(engine, usuario_id)
    async with AsyncSession(engine) as session:
        casa_id = await CasaRepo().get_id_by_nome(session, nome)
    capturas = Capturas() if casa_id is None else await _capturas(engine, usuario_id, casa, casa_id)
    reprocesso = Reprocesso(casa, capturas.guardadas, capturas.ilegiveis, capturas.historicos())
    if sim:
        _enfileirar_todas(usuario_id, reprocesso)
    return reprocesso


def _inicio_do_dia(dia: date | None) -> datetime | None:
    return None if dia is None else datetime.combine(dia, time(), tzinfo=FUSO_DO_BRASIL)


def _fim_do_dia(dia: date | None) -> datetime | None:
    return None if dia is None else _inicio_do_dia(dia + timedelta(days=1))


def _mostrar_plano(evento: str, plano: Plano, escopo: str, **contexto: object) -> None:
    structlog.get_logger().info(
        evento,
        **contexto,
        escopo=escopo,
        usuarios=plano.usuarios,
        apostas=plano.apostas,
        bilhetes_para_a_ia=plano.bilhetes,
        fora_do_escopo=plano.fora_do_escopo,
        sem_releitura=plano.sem_releitura,
        midias_ausentes=plano.midias_ausentes,
        enfileiradas=plano.enfileiradas,
        custo_estimado_brl=round(em_reais(plano.custo_usd), 2),
        custo_estimado_usd=round(plano.custo_usd, 2),
        preco_por_bilhete=ORIGEM_DA_REFERENCIA,
        recado=SEM_FOTOS,
    )


def _mostrar_reprocesso(reprocesso: Reprocesso, sim: bool) -> None:
    if reprocesso.apostas:
        recado = CONFIRA if sim else SEM_SIM
    else:
        recado = NADA_LEGIVEL if reprocesso.guardadas else NADA_GUARDADO
    structlog.get_logger().info(
        "reprocessar_coleta",
        casa=reprocesso.casa,
        pedida=reprocesso.pedida,
        guardadas=reprocesso.guardadas,
        ilegiveis=reprocesso.ilegiveis,
        apostas=len(reprocesso.apostas),
        capturas=sum(len(historico) for historico in reprocesso.apostas),
        enfileiradas=reprocesso.enfileiradas,
        custo_estimado_brl=0.0,
        recado=recado,
    )


def analisador() -> argparse.ArgumentParser:
    modo = argparse.ArgumentParser(add_help=False)
    acao = modo.add_mutually_exclusive_group()
    acao.add_argument("--dry-run", action="store_true", help="só mostra o que seria feito (padrão)")
    acao.add_argument("--sim", action="store_true", help="autoriza o reprocessamento")

    parser = argparse.ArgumentParser(
        prog="reprocessar_usuario",
        description="Mostra o que seria relido ou reprocessado e quanto custa; só age com --sim.",
    )
    comandos = parser.add_subparsers(dest="comando", required=True)

    usuario = comandos.add_parser(
        "usuario", parents=[modo], help="as apostas do Telegram de um usuário"
    )
    usuario.add_argument("--usuario-id", type=int, required=True)
    usuario.add_argument("--desde", type=date.fromisoformat, help="primeiro dia, AAAA-MM-DD")
    usuario.add_argument("--ate", type=date.fromisoformat, help="último dia, AAAA-MM-DD")
    usuario.add_argument("--versao-prompt")
    usuario.add_argument(
        "--tudo", action="store_true", help="todas as apostas, não só as marcadas para revisão"
    )

    todas = comandos.add_parser(
        "reler-todas", parents=[modo], help="as apostas do Telegram de todos os usuários"
    )
    todas.add_argument("--versao-prompt", required=True)
    todas.add_argument("--chunk-size", type=int, default=PAGINA)
    todas.add_argument(
        "--tudo", action="store_true", help="todas as apostas, não só as marcadas para revisão"
    )

    coleta = comandos.add_parser("coleta", parents=[modo], help="o cru guardado da coleta casa")
    coleta.add_argument("--usuario-id", type=int, required=True)
    alvo = coleta.add_mutually_exclusive_group(required=True)
    alvo.add_argument("--coleta-id", type=int)
    alvo.add_argument("--casa")
    return parser


async def _rodar(opcoes: argparse.Namespace) -> int:
    engine = get_engine()
    escopo = ESCOPO_TUDO if getattr(opcoes, "tudo", False) else ESCOPO_REVISAO
    try:
        if opcoes.comando == "usuario":
            plano = await reprocessar_usuario(
                engine,
                opcoes.usuario_id,
                _inicio_do_dia(opcoes.desde),
                _fim_do_dia(opcoes.ate),
                opcoes.versao_prompt,
                escopo,
                sim=opcoes.sim,
            )
            _mostrar_plano("reprocessar_usuario", plano, escopo, usuario_id=opcoes.usuario_id)
        elif opcoes.comando == "reler-todas":
            plano = await reler_todas(
                engine,
                opcoes.versao_prompt,
                escopo,
                chunk_size=opcoes.chunk_size,
                sim=opcoes.sim,
            )
            _mostrar_plano("reler_todas", plano, escopo)
        elif opcoes.coleta_id is not None:
            reprocesso = await reprocessar_coleta(
                engine, opcoes.usuario_id, opcoes.coleta_id, sim=opcoes.sim
            )
            _mostrar_reprocesso(reprocesso, opcoes.sim)
        else:
            reprocesso = await reprocessar_casa(
                engine, opcoes.usuario_id, opcoes.casa, sim=opcoes.sim
            )
            _mostrar_reprocesso(reprocesso, opcoes.sim)
    except (
        VersaoDoPromptError,
        UsuarioNaoEncontradoError,
        ColetaInvalidaError,
        ColetaNaoEncontradaError,
        FilaForaDoArError,
        ValueError,
    ) as recusa:
        structlog.get_logger().error("reprocessamento_recusado", recado=str(recusa))
        return 1
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_rodar(analisador().parse_args(argv)))
