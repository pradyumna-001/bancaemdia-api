"""Authenticated selection options and explicit private bet groups."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, literal, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, PaginationResponse
from bancaemdia.api.deps import get_current_user, get_current_user_snapshot
from bancaemdia.db.session import get_db, get_db_snapshot
from bancaemdia.domain.filtros_apostas import BIGINT_MAX
from bancaemdia.domain.registros import Usuario
from bancaemdia.models.aposta import ORIGENS

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)
DimensaoFiltro = Literal[
    "casas",
    "tipsters",
    "mercados",
    "competicoes",
    "origens",
    "grupos",
    "bancas",
    "titulares",
    "contas",
]
IdTexto = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$", max_length=19)]


class OpcaoFiltro(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    nome: str
    ativa: bool


class OpcoesFiltro(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensao: DimensaoFiltro
    data: list[OpcaoFiltro]
    pagination: PaginationResponse


def _privado(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie"


@router.get("/api/v1/filtros/{dimensao}", response_model=OpcoesFiltro)
async def consultar_opcoes(
    dimensao: DimensaoFiltro,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    id: Annotated[str | None, Query(max_length=128)] = None,
    q: Annotated[str | None, Query(max_length=160)] = None,
    incluir_inativas: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OpcoesFiltro:
    _privado(response)
    if id is not None and (q is not None or page != 1):
        raise HTTPException(422, "resolução por id não aceita q nem outra página")
    if dimensao == "origens":
        nomes = {
            "telegram": "Telegram",
            "telegram_bot": "Telegram Bot",
            "print": "Imagem",
            "manual": "Manual",
            "planilha": "Planilha",
            "casa": "Casa",
        }
        opcoes = [
            OpcaoFiltro(id=o, nome=nomes.get(o, o), ativa=True)
            for o in ORIGENS
            if (id is None or id == o)
            and (
                q is None
                or q.casefold() in nomes.get(o, o).casefold()
                or q.casefold() in o.casefold()
            )
        ]
        return OpcoesFiltro(
            dimensao=dimensao,
            data=opcoes[(page - 1) * page_size : page * page_size],
            pagination=PaginationResponse(page=page, page_size=page_size, total=len(opcoes)),
        )
    modelos = {
        "casas": models.Casa,
        "tipsters": models.Tipster,
        "mercados": models.Mercado,
        "competicoes": models.Competicao,
        "grupos": models.GrupoAposta,
        "bancas": models.Banca,
        "titulares": models.Titular,
        "contas": models.ContaCasa,
    }
    entidade: Any = modelos[dimensao]
    ativa: Any = literal(True)
    if dimensao == "casas":
        ativa = entidade.ativa
    elif dimensao in {"grupos", "titulares"}:
        ativa = ~entidade.arquivado
    elif dimensao == "contas":
        ativa = entidade.ativa & (entidade.estado != "ENCERRADA")
    if dimensao == "contas":
        nome = func.concat(models.Casa.nome, " — ", entidade.apelido)
        query = select(entidade.id, nome.label("nome"), ativa.label("ativa")).join(
            models.Casa, models.Casa.id == entidade.casa_id
        )
    else:
        nome = entidade.nome
        query = select(entidade.id, nome.label("nome"), ativa.label("ativa"))
    if dimensao in {"grupos", "bancas", "titulares", "contas"}:
        query = query.where(entidade.usuario_id == usuario.id)
    if id is not None:
        if not id.isascii() or not id.isdecimal() or not 1 <= int(id) <= BIGINT_MAX:
            raise HTTPException(422, "id precisa ser um BIGINT positivo em texto decimal")
        query = query.where(entidade.id == int(id))
    elif not incluir_inativas:
        query = query.where(ativa)
    if q is not None:
        query = query.where(func.strpos(func.lower(nome), q.lower()) > 0)
    try:
        total = int(
            (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        )
        rows = (
            await session.execute(
                query.order_by(nome, entidade.id).limit(page_size).offset((page - 1) * page_size)
            )
        ).mappings()
        data = [
            OpcaoFiltro(id=str(row["id"]), nome=row["nome"], ativa=row["ativa"]) for row in rows
        ]
    except DBAPIError as error:
        raise HTTPException(
            503,
            "catálogo temporariamente indisponível",
            headers={"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"},
        ) from error
    return OpcoesFiltro(
        dimensao=dimensao,
        data=data,
        pagination=PaginationResponse(page=page, page_size=page_size, total=total),
    )


class GrupoEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nome: str = Field(min_length=1, max_length=160)
    arquivado: bool = False

    @field_validator("nome")
    @classmethod
    def nome_valido(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("nome vazio")
        return valor.strip()


class GruposDaAposta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grupo_ids: list[IdTexto] = Field(max_length=100)

    @field_validator("grupo_ids")
    @classmethod
    def ids_validos(cls, valor: list[str]) -> list[str]:
        if len(set(valor)) != len(valor) or any(int(v) > BIGINT_MAX for v in valor):
            raise ValueError("IDs duplicados ou fora de BIGINT")
        return valor


@router.post("/api/v1/grupos", response_model=OpcaoFiltro, status_code=201)
async def criar_grupo(
    entrada: GrupoEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> OpcaoFiltro:
    grupo = models.GrupoAposta(usuario_id=usuario.id, **entrada.model_dump())
    session.add(grupo)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(422, "grupo com esse nome já existe") from error
    return OpcaoFiltro(id=str(grupo.id), nome=grupo.nome, ativa=not grupo.arquivado)


@router.patch("/api/v1/grupos/{grupo_id}", response_model=OpcaoFiltro)
async def editar_grupo(
    grupo_id: Annotated[IdTexto, Path(description="ID decimal exato do grupo, até BIGINT máximo.")],
    entrada: GrupoEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> OpcaoFiltro:
    numero = int(grupo_id)
    if numero > BIGINT_MAX:
        raise HTTPException(422, "grupo_id precisa ser um BIGINT positivo")
    grupo = await session.scalar(
        select(models.GrupoAposta)
        .where(models.GrupoAposta.usuario_id == usuario.id, models.GrupoAposta.id == numero)
        .with_for_update()
    )
    if grupo is None:
        raise HTTPException(404, "grupo não encontrado")
    grupo.nome, grupo.arquivado = entrada.nome, entrada.arquivado
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(422, "grupo com esse nome já existe") from error
    return OpcaoFiltro(id=str(grupo.id), nome=grupo.nome, ativa=not grupo.arquivado)


@router.put("/api/v1/apostas/{chave}/grupos", response_model=GruposDaAposta)
async def vincular_grupos(
    chave: str,
    entrada: GruposDaAposta,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> GruposDaAposta:
    aposta = await session.scalar(
        select(models.Aposta)
        .where(models.Aposta.usuario_id == usuario.id, models.Aposta.chave == chave)
        .with_for_update()
    )
    if aposta is None:
        raise HTTPException(404, "aposta não encontrada")
    ids = [int(v) for v in entrada.grupo_ids]
    encontrados = list(
        (
            await session.execute(
                select(models.GrupoAposta.id)
                .where(
                    models.GrupoAposta.usuario_id == usuario.id,
                    models.GrupoAposta.id.in_(ids),
                    models.GrupoAposta.arquivado.is_(False),
                )
                .order_by(models.GrupoAposta.id)
                .with_for_update()
            )
        ).scalars()
    )
    if set(encontrados) != set(ids):
        raise HTTPException(422, "grupo indisponível para associação")
    await session.execute(
        delete(models.ApostaGrupo).where(
            models.ApostaGrupo.usuario_id == usuario.id, models.ApostaGrupo.aposta_id == aposta.id
        )
    )
    session.add_all([
        models.ApostaGrupo(usuario_id=usuario.id, aposta_id=aposta.id, grupo_id=g) for g in ids
    ])
    await session.commit()
    return entrada
