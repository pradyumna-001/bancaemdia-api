from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.deps import get_current_user
from bancaemdia.db.session import get_db
from bancaemdia.domain.aposta_service import (
    CAMPOS_DE_ID,
    ApostaInvalidaError,
    eventos_da_correcao,
    eventos_da_exclusao,
    eventos_da_restauracao,
    eventos_do_resultado,
    mudancas,
    validar_correcao,
    validar_resultado,
)
from bancaemdia.domain.financeiro import Aposta as ApostaFinanceira
from bancaemdia.domain.materializar import EventoNovo, casa_canonica, projetar
from bancaemdia.domain.registros import Aposta, Usuario
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.models import Competicao, Mercado, Time, Tipster
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.workers.materialization import FUSO_DO_BRASIL

TAMANHO_MAXIMO_DA_PAGINA = 100
TAMANHO_PADRAO_DA_PAGINA = 50
OCUPADA = "esta aposta está sendo lida agora — tente de novo em instantes"
NAO_ACHEI = "não achei esta aposta"
SO_EM_REVISAO = (
    "o estado só pode ser corrigido por aqui numa aposta marcada para revisão — use"
    " POST /api/v1/apostas/{chave}/resultado"
)

router = APIRouter()


class Correcao(BaseModel):
    # `extra="allow"` deixa a lista de campos ser a do domínio: o que não pode ser corrigido é
    # recusado com o nome do campo, em vez de sumir calado.
    model_config = ConfigDict(extra="allow")


class Resultado(BaseModel):
    estado: str
    retorno_centavos: int | None = None
    cashout_valor_centavos: int | None = None
    comissao_centavos: int | None = None


class ApostaManual(BaseModel):
    casa: str | None = None
    data_aposta: str | None = None
    odd: float | None = None
    stake_unidades: float | None = None
    evento: str | None = None
    descricao: str | None = None
    mercado_bruto: str | None = None
    freebet: bool = False
    comissao_centavos: int | None = None


def erro(status_code: int, mensagem: str) -> JSONResponse:
    # O corpo de erro do FastAPI é uma lista em inglês; quem lê é a pessoa, então a validação é
    # nossa e a frase é a mesma do projeto antigo.
    return JSONResponse(status_code=status_code, content={"detail": mensagem})


def _quando(valor: datetime | None) -> str | None:
    return None if valor is None else valor.isoformat()


def linha_da_aposta(aposta: Aposta, estado: dict[str, Any] | None = None) -> dict[str, Any]:
    atual = estado or {}
    return {
        "chave": aposta.chave,
        "origem": aposta.origem,
        "estado": aposta.estado,
        "odd": aposta.odd,
        "stake_unidades": aposta.stake_unidades,
        "stake_centavos": aposta.stake_centavos,
        "valor_aposta_centavos": aposta.valor_aposta_centavos,
        "retorno_centavos": aposta.retorno_centavos,
        # O lucro sai da própria linha: recalcular a stake por divisão dava um número que
        # discordava do `stake_centavos` do mesmo corpo.
        "lucro_centavos": (
            None
            if aposta.retorno_centavos is None
            else aposta.retorno_centavos - aposta.stake_centavos
        ),
        "freebet": aposta.freebet,
        "conta_casa_id": aposta.conta_casa_id,
        "tipster_id": aposta.tipster_id,
        "time_casa_id": aposta.time_casa_id,
        "time_fora_id": aposta.time_fora_id,
        "mercado_id": aposta.mercado_id,
        "competicao_id": aposta.competicao_id,
        "data_aposta": _quando(aposta.data_aposta),
        "data_jogo": _quando(aposta.data_jogo),
        "chat_id": aposta.chat_id,
        "message_id": aposta.message_id,
        "midia_hash": aposta.midia_hash,
        "revisao_grave": aposta.revisao_grave,
        "apagada": not aposta.selecionada,
        # O texto da aposta mora no histórico, não na linha: sem isto, corrigir a descrição ou a
        # casa não mudava nada do que a pessoa vê.
        "casa": atual.get("casa"),
        "evento": atual.get("evento"),
        "descricao": atual.get("descricao"),
        "mercado": atual.get("mercado_bruto"),
        "criada_em": _quando(aposta.criada_em),
        "atualizada_em": _quando(aposta.atualizada_em),
    }


async def _travar(session: AsyncSession, usuario_id: int, chave: str) -> bool:
    # A mesma chave que o trabalhador toma ao gravar uma leitura: sem ela, a releitura e a
    # correção leem o mesmo histórico e uma das duas some. Não espera, porque o primário corta
    # qualquer consulta em 5 s.
    livre = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"{usuario_id}:{chave}"},
    )
    return bool(livre)


async def _aplicar(
    session: AsyncSession,
    usuario: Usuario,
    chave: str,
    novos: list[EventoNovo],
    atual: dict[str, Any],
) -> Aposta | None:
    eventos = EventoRepo()
    for evento in novos:
        await eventos.append(
            session,
            {
                "usuario_id": usuario.id,
                "tipo": evento.tipo,
                "fonte": evento.fonte,
                "payload_json": evento.payload,
                "confianca": evento.confianca,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": chave,
            },
        )
    financeira = ApostaFinanceira(
        stake_unidades=float(atual.get("stake_unidades") or 0.0),
        valor_unidade_centavos=int(
            atual.get("valor_unidade_centavos") or VALOR_UNIDADE_PADRAO_CENTAVOS
        ),
        freebet=bool(atual.get("freebet")),
    )
    dados: dict[str, object] = {
        "usuario_id": usuario.id,
        "chave": chave,
        # A origem e a mensagem vêm do histórico: o upsert monta um INSERT completo antes de
        # descobrir que a linha já existe, e sem elas ele quebraria numa coluna obrigatória.
        "origem": atual.get("origem", "manual"),
        "stake_unidades": financeira.stake_unidades,
        "stake_centavos": financeira.stake_centavos,
        "valor_aposta_centavos": financeira.valor_aposta_centavos,
        "odd": atual.get("odd"),
        "freebet": financeira.freebet,
        "estado": atual.get("estado", "PENDENTE"),
        "retorno_centavos": atual.get("retorno_centavos"),
        "revisao_grave": bool(atual.get("revisao_grave")),
        "selecionada": bool(atual.get("selecionada", True)),
        "data_aposta": _data_do_estado(atual.get("data_aposta")),
        "data_jogo": _data_do_estado(atual.get("data_jogo")),
    }
    # A chave manda, não o valor: com `is not None`, limpar um id deixava a linha com o valor
    # velho enquanto o histórico já dizia que ele saiu.
    for campo in CAMPOS_DE_ID:
        if campo in atual:
            dados[campo] = atual[campo]
    for campo in ("chat_id", "message_id", "ordem_na_mensagem", "midia_hash"):
        if atual.get(campo) is not None:
            dados[campo] = atual[campo]
    return await ApostaRepo().upsert_materializada(session, dados)


def _data_do_estado(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        data = datetime.fromisoformat(valor)
    except ValueError:
        return None
    # A mesma regra do trabalhador: data sem fuso é hora do Brasil. Com dois parsers diferentes,
    # um PATCH movia a aposta três horas — e às vezes de dia.
    return data if data.tzinfo else data.replace(tzinfo=FUSO_DO_BRASIL)


async def _conferir_ids(
    session: AsyncSession, usuario_id: int, pedido: dict[str, Any]
) -> str | None:
    conta_casa_id = pedido.get("conta_casa_id")
    if (
        conta_casa_id is not None
        and isinstance(conta_casa_id, int)
        and not isinstance(conta_casa_id, bool)
    ):
        # Um id que não é da pessoa viraria erro 500 na chave estrangeira; e sob a RLS ele nem
        # existe. Esta é a única referência deste grupo que pertence a um usuário.
        if await ContaCasaRepo().get_by_id(session, usuario_id, int(conta_casa_id)) is None:
            return "conta_casa_id não existe nas suas contas"

    # Estes catálogos são vocabulário canônico compartilhado, deliberadamente sem usuario_id e sem
    # RLS (ADR 014, issue 7). Não há referência cross-tenant possível nesses campos; ainda assim
    # validamos a existência antes de escrever para transformar um FK inválido em 422.
    referencias = (
        ("tipster_id", Tipster.id),
        ("time_casa_id", Time.id),
        ("time_fora_id", Time.id),
        ("mercado_id", Mercado.id),
        ("competicao_id", Competicao.id),
    )
    for campo, coluna in referencias:
        valor = pedido.get(campo)
        # O domínio produz a mensagem de tipo amigável; não tente consultar um valor inválido.
        if (
            valor is not None
            and isinstance(valor, int)
            and not isinstance(valor, bool)
            and (await session.scalar(select(coluna).where(coluna == valor)) is None)
        ):
            return f"{campo} não existe no catálogo compartilhado"
    return None


async def _historico(session: AsyncSession, usuario_id: int, chave: str) -> list[Any]:
    return [
        (e.tipo, e.fonte, e.payload_json)
        for e in await EventoRepo().list_by_aposta_chave(session, usuario_id, chave)
    ]


@router.get("/api/v1/apostas")
async def listar_apostas(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    desde: datetime | None = None,
    ate: datetime | None = None,
    estado: str | None = None,
    casa_id: int | None = None,
    tipster_id: int | None = None,
    mercado_id: int | None = None,
    competicao_id: int | None = None,
    origem: str | None = None,
    revisao_grave: bool | None = None,
    incluir_apagadas: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=TAMANHO_MAXIMO_DA_PAGINA)] = TAMANHO_PADRAO_DA_PAGINA,
) -> JSONResponse:
    apostas, total = await ApostaRepo().list_page(
        session,
        usuario.id,
        {
            "desde": desde,
            "ate": ate,
            "estado": estado,
            "casa_id": casa_id,
            "tipster_id": tipster_id,
            "mercado_id": mercado_id,
            "competicao_id": competicao_id,
            "origem": origem,
            "revisao_grave": revisao_grave,
            "incluir_apagadas": incluir_apagadas,
        },
        page,
        page_size,
    )
    return JSONResponse({
        "data": [linha_da_aposta(aposta) for aposta in apostas],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    })


@router.get("/api/v1/apostas/{chave}")
async def ver_aposta(
    chave: str,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    aposta = await ApostaRepo().get_by_chave(session, usuario.id, chave)
    if aposta is None:
        return erro(status.HTTP_404_NOT_FOUND, NAO_ACHEI)
    eventos = await EventoRepo().list_by_aposta_chave(session, usuario.id, chave)
    revisoes = await RevisaoPendenteRepo().list_abertas_by_aposta_chave(session, usuario.id, chave)
    atual, _ = projetar([(e.tipo, e.fonte, e.payload_json) for e in eventos])
    return JSONResponse({
        "aposta": linha_da_aposta(aposta, atual),
        # `selecoes` não é tabela: o que o motor guardou das pernas do bilhete é o texto da
        # descrição e o mercado. Vem do histórico dobrado, não do evento de criação, senão a
        # correção da pessoa não apareceria em lugar nenhum.
        "selecoes": {
            "descricao": atual.get("descricao"),
            "mercado": atual.get("mercado_bruto"),
            "evento": atual.get("evento"),
            "casa": atual.get("casa"),
        },
        "eventos": [
            {
                "tipo": e.tipo,
                "fonte": e.fonte,
                "payload": e.payload_json,
                "confianca": e.confianca,
                "criado_em": _quando(e.criado_em),
            }
            for e in eventos
        ],
        "revisao_pendente": (
            None
            if not revisoes
            else {
                "id": revisoes[0].id,
                "motivo": revisoes[0].motivo,
                "midia_hash": revisoes[0].midia_hash,
                "criado_em": _quando(revisoes[0].criado_em),
            }
        ),
    })


async def _escrever(
    session: AsyncSession,
    usuario: Usuario,
    chave: str,
    monta: Callable[[dict[str, Any]], list[EventoNovo]],
) -> JSONResponse:
    if not await _travar(session, usuario.id, chave):
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, OCUPADA)
    if await ApostaRepo().get_by_chave_for_update(session, usuario.id, chave) is None:
        await session.rollback()
        return erro(status.HTTP_404_NOT_FOUND, NAO_ACHEI)
    historico = await _historico(session, usuario.id, chave)
    antes, _ = projetar(historico)
    try:
        novos = monta(antes)
    except ApostaInvalidaError as recusa:
        await session.rollback()
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, str(recusa))
    depois, _ = projetar([*historico, *((e.tipo, e.fonte, e.payload) for e in novos)])
    gravada = await _aplicar(session, usuario, chave, novos, depois)
    if gravada is None:
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, OCUPADA)
    saiu_da_conta = antes.get("selecionada", True) and not depois.get("selecionada", True)
    # Quem apagou a aposta não tem nada a confirmar: a revisão dela sai da fila junto.
    if (antes.get("revisao_motivo") and not depois.get("revisao_motivo")) or saiu_da_conta:
        await RevisaoPendenteRepo().resolve_superseded(session, usuario.id, chave, None)
    await session.commit()
    return JSONResponse({
        "aposta": linha_da_aposta(gravada, depois),
        "eventos_gravados": len(novos),
    })


@router.patch("/api/v1/apostas/{chave}")
async def corrigir_aposta(
    chave: str,
    correcao: Correcao,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    # O que a pessoa não mandou não é `None`: é "não falei disso". Campo ausente nunca apaga o
    # que já estava gravado (lição do projeto antigo).
    pedido = correcao.model_dump(exclude_unset=True)

    recado = await _conferir_ids(session, usuario.id, pedido)
    if recado is not None:
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, recado)

    def monta(antes: dict[str, Any]) -> list[EventoNovo]:
        if "estado" in pedido and not antes.get("revisao_grave"):
            raise ApostaInvalidaError(SO_EM_REVISAO)
        validar_correcao(pedido, antes)
        return eventos_da_correcao(mudancas(antes, pedido))

    return await _escrever(session, usuario, chave, monta)


@router.post("/api/v1/apostas/{chave}/resultado")
async def registrar_resultado(
    chave: str,
    resultado: Resultado,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    def monta(antes: dict[str, Any]) -> list[EventoNovo]:
        validar_resultado(
            resultado.estado,
            resultado.retorno_centavos,
            resultado.cashout_valor_centavos,
            resultado.comissao_centavos,
        )
        # O cashout tem um nome só dentro do motor: o valor que a casa pagou é o retorno.
        retorno = (
            resultado.cashout_valor_centavos
            if resultado.estado == "CASHOUT" and resultado.cashout_valor_centavos is not None
            else resultado.retorno_centavos
        )
        # Repetir o mesmo resultado sem redigitar o valor não apaga o que a casa pagou: "não falei
        # disso" é manter. Trocar de estado, sim, volta para a fórmula.
        if (
            retorno is None
            and antes.get("retorno_informado")
            and antes.get("estado") == resultado.estado
        ):
            retorno = antes.get("retorno_centavos")
        return eventos_do_resultado(resultado.estado, retorno, resultado.comissao_centavos)

    return await _escrever(session, usuario, chave, monta)


@router.delete("/api/v1/apostas/{chave}")
async def apagar_aposta(
    chave: str,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    def monta(antes: dict[str, Any]) -> list[EventoNovo]:
        # Segundo clique não empilha evento: apagar o que já está apagado não é fato novo.
        return [] if not antes.get("selecionada", True) else eventos_da_exclusao()

    return await _escrever(session, usuario, chave, monta)


@router.post("/api/v1/apostas/{chave}/restaurar")
async def restaurar_aposta(
    chave: str,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    def monta(antes: dict[str, Any]) -> list[EventoNovo]:
        return [] if antes.get("selecionada", True) else eventos_da_restauracao()

    return await _escrever(session, usuario, chave, monta)


@router.post("/api/v1/apostas", status_code=status.HTTP_201_CREATED)
async def criar_aposta(
    manual: ApostaManual,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    nome = casa_canonica(manual.casa or "")
    if not manual.casa or not manual.casa.strip():
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, "falta a casa de apostas")
    if nome is None:
        return erro(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"não conheço a casa {manual.casa!r} — confira o nome",
        )
    try:
        validar_correcao(
            {"odd": manual.odd, "stake_unidades": manual.stake_unidades},
            {},
        )
    except ApostaInvalidaError as recusa:
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, str(recusa))

    # A chave manual não vem de mensagem nenhuma; os prefixos `t:` e `c:` já dizem de onde as
    # outras vieram.
    chave = f"m:{uuid4().hex}"
    data_aposta = _data_do_estado(manual.data_aposta) or datetime.now(FUSO_DO_BRASIL)
    unidade = await UnidadeRepo().get_vigente(session, usuario.id, data_aposta)
    valor_unidade = VALOR_UNIDADE_PADRAO_CENTAVOS if unidade is None else unidade.valor_centavos
    casa_id = await CasaRepo().get_id_by_nome(session, nome)
    conta = await ContaCasaRepo().get_vigente_by_nome_da_casa(
        session, usuario.id, nome, data_aposta
    )
    payload: dict[str, Any] = {
        "origem": "manual",
        "casa": nome,
        "evento": manual.evento,
        "descricao": manual.descricao,
        "mercado_bruto": manual.mercado_bruto,
        "odd": manual.odd,
        "stake_unidades": manual.stake_unidades,
        "valor_unidade_centavos": valor_unidade,
        "freebet": manual.freebet,
        "selecionada": True,
        "data_aposta": data_aposta.isoformat(),
        "revisao_grave": False,
    }
    if manual.comissao_centavos is not None:
        payload["comissao_centavos"] = manual.comissao_centavos
    if conta is not None:
        payload["conta_casa_id"] = conta.id

    novos = [EventoNovo("APOSTA_CRIADA", "manual", payload)]
    depois, _ = projetar([(e.tipo, e.fonte, e.payload) for e in novos])
    gravada = await _aplicar(session, usuario, chave, novos, depois)
    if gravada is None:
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, OCUPADA)
    await session.commit()
    corpo: dict[str, Any] = {"aposta": linha_da_aposta(gravada, depois), "casa_id": casa_id}
    if conta is None:
        # O filtro por casa passa pelas contas: sem conta, a aposta existe e não aparece nele.
        corpo["aviso"] = (
            f"você ainda não tem conta na {nome} — esta aposta entrou, mas não aparece no filtro"
            " por casa até a conta existir"
        )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=corpo)
