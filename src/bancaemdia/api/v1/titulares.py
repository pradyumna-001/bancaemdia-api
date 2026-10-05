"""Explicit preview and application of an account holder switch."""

from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user, get_current_user_snapshot
from bancaemdia.db.session import get_db, get_db_snapshot
from bancaemdia.domain.account_financials import AccountMetrics
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.titular_matrix import account_row
from bancaemdia.domain.titulares import (
    ChaveIdempotenciaEmConflitoError,
    TrocaInvalidaError,
    TrocaPedido,
    trocar_conta,
)
from bancaemdia.repositories.account_financials_repo import AccountFinancialsRepo
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo
from bancaemdia.repositories.titular_repo import TitularRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class TrocaContaEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    casa_id: int = Field(strict=True, ge=1)
    conta_origem_id: int = Field(strict=True, ge=1)
    conta_destino_id: int = Field(strict=True, ge=1)
    efetiva_em: datetime
    estado_origem: Literal["DISPONIVEL", "LIMITADA", "ENCERRADA"]

    @field_validator("efetiva_em")
    @classmethod
    def exige_fuso(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("efetiva_em deve ter fuso horário")
        return value


class TrocaContaSaida(BaseModel):
    casa_id: int
    conta_origem_id: int
    conta_destino_id: int
    efetiva_em: datetime
    estado_origem: Literal["DISPONIVEL", "LIMITADA", "ENCERRADA"]
    apostas_afetadas_ids: list[int]
    aplicada: bool


async def _switch(
    entrada: TrocaContaEntrada,
    chave: str,
    usuario: Usuario,
    session: AsyncSession,
    *,
    aplicar: bool,
) -> TrocaContaSaida:
    pedido = TrocaPedido(
        casa_id=entrada.casa_id,
        conta_origem_id=entrada.conta_origem_id,
        conta_destino_id=entrada.conta_destino_id,
        efetiva_em=entrada.efetiva_em,
        estado_origem=entrada.estado_origem,
    )
    try:
        result = await trocar_conta(session, usuario.id, pedido, chave, aplicar=aplicar)
    except ChaveIdempotenciaEmConflitoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TrocaInvalidaError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return TrocaContaSaida.model_validate(result)


@router.post(
    "/api/v1/titulares/trocas/preview",
    response_model=TrocaContaSaida,
    summary="Prévia de troca de conta da casa",
    description="Mostra IDs das apostas existentes que seriam afetadas por uma troca retroativa.",
)
async def preview_troca(
    entrada: TrocaContaEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    chave: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=160)],
) -> TrocaContaSaida:
    return await _switch(entrada, chave, usuario, session, aplicar=False)


@router.post(
    "/api/v1/titulares/trocas",
    response_model=TrocaContaSaida,
    status_code=status.HTTP_200_OK,
    summary="Trocar conta da casa",
    description="Fecha o uso atual e abre o destino no mesmo instante, sob travas transacionais.",
)
async def aplicar_troca(
    entrada: TrocaContaEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    chave: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=160)],
) -> TrocaContaSaida:
    return await _switch(entrada, chave, usuario, session, aplicar=True)


class TitularEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nome: str = Field(min_length=1, max_length=160)

    @field_validator("nome")
    @classmethod
    def nome_nao_vazio(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nome não pode ser vazio")
        return value.strip()


class TitularSaida(BaseModel):
    id: int
    nome: str
    arquivado: bool
    criado_em: datetime


class TitularesPaginaSaida(BaseModel):
    data: list[TitularSaida]
    page: int
    page_size: int
    total: int


class ContaEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")
    casa_id: int = Field(strict=True, ge=1)
    apelido: str = Field(min_length=1, max_length=160)

    @field_validator("apelido")
    @classmethod
    def apelido_nao_vazio(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("apelido não pode ser vazio")
        return value.strip()


class ContaEdicao(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apelido: str | None = Field(default=None, min_length=1, max_length=160)
    estado: Literal["DISPONIVEL", "LIMITADA", "ENCERRADA"] | None = None

    @model_validator(mode="after")
    def exige_mudanca(self) -> "ContaEdicao":
        if self.apelido is None and self.estado is None:
            raise ValueError("informe apelido ou estado")
        if self.apelido is not None:
            self.apelido = ContaEntrada.apelido_nao_vazio(self.apelido)
        return self


class IntervaloSaida(BaseModel):
    vigente_de: datetime | None
    vigente_ate: datetime | None
    origem: Literal["LEGADO", "EXPLICITA"]


class ContaMatrizSaida(BaseModel):
    conta_casa_id: int
    titular_id: int | None
    titular_nome: str | None
    casa_id: int
    casa_nome: str
    apelido: str
    estado: Literal["DISPONIVEL", "EM_USO", "LIMITADA", "ENCERRADA"]
    ativa: bool
    intervalo_ativo: IntervaloSaida | None
    historico_uso: list[IntervaloSaida]
    acoes_validas: list[Literal["ATIVAR", "TROCAR_PARA", "TROCAR_DE", "EDITAR"]]


class MatrizTitularSaida(BaseModel):
    titular: TitularSaida
    contas: list[ContaMatrizSaida]


class MatrizCasaSaida(BaseModel):
    casa_id: int
    casa_nome: str
    contas: list[ContaMatrizSaida]
    titulares_sem_conta: list[TitularSaida]
    proxima_acao_sem_conta: Literal["CRIAR_CONTA"]


class ContaFinanceiraSaida(BaseModel):
    conta_casa_id: int
    titular_id: int | None
    casa_id: int
    casa_nome: str
    apelido: str
    metricas: dict[str, int | str]
    saldo_atual_centavos: int | None


class TitularFinanceiroSaida(BaseModel):
    titular_id: int
    nome: str
    metricas: dict[str, int | str]
    saldo_atual_centavos: int | None
    contas: list[ContaFinanceiraSaida]


class FinanceiroSaida(BaseModel):
    total: dict[str, int | str]
    nao_atribuidas: dict[str, int | str]
    nao_atribuidas_status: Literal["UNASSIGNED"]
    titulares: list[TitularFinanceiroSaida]
    contas_sem_titular: list[ContaFinanceiraSaida]
    saldo_atual_centavos: int | None


def _holder(value: models.Titular) -> TitularSaida:
    return TitularSaida(
        id=value.id, nome=value.nome, arquivado=value.arquivado, criado_em=value.criado_em
    )


async def _account_rows(
    session: AsyncSession, usuario_id: int, accounts: list[models.ContaCasa]
) -> list[ContaMatrizSaida]:
    houses = {
        house.id: house.nome for house in (await session.execute(select(models.Casa))).scalars()
    }
    holders = {holder.id: holder for holder in await TitularRepo().list_all(session, usuario_id)}
    usages = await UsoContaCasaRepo().list_by_accounts(
        session, usuario_id, [a.id for a in accounts]
    )
    by_account: dict[int, list[models.UsoContaCasa]] = defaultdict(list)
    current_houses: set[int] = set()
    for usage in usages:
        by_account[usage.conta_casa_id].append(usage)
        if usage.vigente_ate is None:
            current_houses.add(usage.casa_id)
    return [
        ContaMatrizSaida.model_validate(
            account_row(
                account,
                houses[account.casa_id],
                holders[account.titular_id].nome if account.titular_id in holders else None,
                holders[account.titular_id].arquivado if account.titular_id in holders else True,
                by_account[account.id],
                another_current=account.casa_id in current_houses,
            )
        )
        for account in accounts
    ]


@router.post("/api/v1/titulares", response_model=TitularSaida, status_code=201)
async def criar_titular(
    entrada: TitularEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TitularSaida:
    holder = await TitularRepo().create(session, usuario.id, entrada.nome)
    await session.commit()
    return _holder(holder)


@router.get("/api/v1/titulares", response_model=TitularesPaginaSaida)
async def listar_titulares(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    search: Annotated[str | None, Query(max_length=160)] = None,
    include_archived: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TitularesPaginaSaida:
    holders, total = await TitularRepo().list_page(
        session,
        usuario.id,
        search=search,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )
    return TitularesPaginaSaida(
        data=[_holder(holder) for holder in holders],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/api/v1/titulares/casas/{casa_id}/matriz", response_model=MatrizCasaSaida)
async def matriz_casa(
    casa_id: int,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
) -> MatrizCasaSaida:
    house_name = await CasaRepo().get_nome_by_id(session, casa_id)
    if house_name is None:
        raise HTTPException(404, "não achei esta casa")
    accounts = await ContaCasaRepo().list_by_house(session, usuario.id, casa_id)
    holders = await TitularRepo().list_all(session, usuario.id)
    account_holders = {account.titular_id for account in accounts}
    return MatrizCasaSaida(
        casa_id=casa_id,
        casa_nome=house_name,
        contas=await _account_rows(session, usuario.id, accounts),
        titulares_sem_conta=[
            _holder(holder)
            for holder in holders
            if holder.id not in account_holders and not holder.arquivado
        ],
        proxima_acao_sem_conta="CRIAR_CONTA",
    )


@router.get("/api/v1/titulares/financeiro", response_model=FinanceiroSaida)
async def consultar_financeiro(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    desde: datetime | None = None,
    ate: datetime | None = None,
    casa_id: Annotated[int | None, Query(ge=1)] = None,
    titular_id: Annotated[int | None, Query(ge=1)] = None,
    conta_casa_id: Annotated[int | None, Query(ge=1)] = None,
) -> FinanceiroSaida:
    if any(
        value is not None and (value.tzinfo is None or value.utcoffset() is None)
        for value in (desde, ate)
    ) or (desde is not None and ate is not None and desde >= ate):
        raise HTTPException(422, "período exige fuso horário e fim posterior ao início")
    holders = {holder.id: holder for holder in await TitularRepo().list_all(session, usuario.id)}
    accounts = await ContaCasaRepo().list_by_usuario(session, usuario.id)
    account_map = {account.id: account for account in accounts}
    if titular_id is not None and titular_id not in holders:
        raise HTTPException(404, "não achei este titular")
    if conta_casa_id is not None and conta_casa_id not in account_map:
        raise HTTPException(404, "não achei esta conta")
    if casa_id is not None and await CasaRepo().get_nome_by_id(session, casa_id) is None:
        raise HTTPException(404, "não achei esta casa")
    if conta_casa_id is not None:
        account = account_map[conta_casa_id]
        if (titular_id is not None and account.titular_id != titular_id) or (
            casa_id is not None and account.casa_id != casa_id
        ):
            raise HTTPException(422, "filtros de titular, casa e conta não correspondem")
    filtered_accounts = [
        account
        for account in accounts
        if (casa_id is None or account.casa_id == casa_id)
        and (titular_id is None or account.titular_id == titular_id)
        and (conta_casa_id is None or account.id == conta_casa_id)
    ]
    repo = AccountFinancialsRepo()
    bets = {
        row["conta_casa_id"]: row
        for row in await repo.bets(
            session,
            usuario.id,
            desde=desde,
            ate=ate,
            casa_id=casa_id,
            titular_id=titular_id,
            conta_casa_id=conta_casa_id,
        )
    }
    movements = {
        row["conta_casa_id"]: row
        for row in await repo.movements(
            session,
            usuario.id,
            desde=desde,
            ate=ate,
            casa_id=casa_id,
            titular_id=titular_id,
            conta_casa_id=conta_casa_id,
        )
    }
    balances = await MovimentoRepo().aggregate_saldos_by_usuario(session, usuario.id)
    house_names = {
        house.id: house.nome for house in (await session.execute(select(models.Casa))).scalars()
    }
    by_holder: dict[int, list[ContaFinanceiraSaida]] = defaultdict(list)
    no_holder: list[ContaFinanceiraSaida] = []
    account_metrics: list[AccountMetrics] = []
    metrics_by_account: dict[int, AccountMetrics] = {}
    for account in filtered_accounts:
        metrics = AccountMetrics.from_rows(bets.get(account.id), movements.get(account.id))
        account_metrics.append(metrics)
        metrics_by_account[account.id] = metrics
        balance = balances.get(account.id)
        item = ContaFinanceiraSaida(
            conta_casa_id=account.id,
            titular_id=account.titular_id,
            casa_id=account.casa_id,
            casa_nome=house_names[account.casa_id],
            apelido=account.apelido,
            metricas=metrics.as_dict(),
            saldo_atual_centavos=None if balance is None else balance.saldo_centavos,
        )
        if account.titular_id is None:
            no_holder.append(item)
        else:
            by_holder[account.titular_id].append(item)
    holder_rows: list[TitularFinanceiroSaida] = []
    for holder_id, holder_accounts in sorted(by_holder.items(), key=lambda pair: pair[0]):
        values = [item.saldo_atual_centavos for item in holder_accounts]
        holder_rows.append(
            TitularFinanceiroSaida(
                titular_id=holder_id,
                nome=holders[holder_id].nome,
                metricas=AccountMetrics.sum([
                    metrics_by_account[item.conta_casa_id] for item in holder_accounts
                ]).as_dict(),
                saldo_atual_centavos=sum(value for value in values if value is not None)
                if all(value is not None for value in values)
                else None,
                contas=holder_accounts,
            )
        )
    unassigned = AccountMetrics.from_rows(bets.get(None), movements.get(None))
    all_metrics = AccountMetrics.sum([*account_metrics, unassigned])
    values = [item.saldo_atual_centavos for row in [*holder_rows] for item in row.contas]
    values.extend(item.saldo_atual_centavos for item in no_holder)
    return FinanceiroSaida(
        total=all_metrics.as_dict(),
        nao_atribuidas=unassigned.as_dict(),
        nao_atribuidas_status="UNASSIGNED",
        titulares=holder_rows,
        contas_sem_titular=no_holder,
        saldo_atual_centavos=sum(value for value in values if value is not None)
        if all(value is not None for value in values)
        else None,
    )


@router.get("/api/v1/titulares/{titular_id}", response_model=TitularSaida)
async def obter_titular(
    titular_id: int,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
) -> TitularSaida:
    holder = await TitularRepo().get_by_id(session, usuario.id, titular_id)
    if holder is None:
        raise HTTPException(404, "não achei este titular")
    return _holder(holder)


@router.patch("/api/v1/titulares/{titular_id}", response_model=TitularSaida)
async def editar_titular(
    titular_id: int,
    entrada: TitularEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TitularSaida:
    holder = await TitularRepo().update_name(session, usuario.id, titular_id, entrada.nome)
    if holder is None:
        raise HTTPException(404, "não achei este titular ativo")
    await session.commit()
    return _holder(holder)


@router.delete("/api/v1/titulares/{titular_id}", response_model=TitularSaida)
async def arquivar_titular(
    titular_id: int,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TitularSaida:
    await session.execute(
        select(models.Usuario.id).where(models.Usuario.id == usuario.id).with_for_update()
    )
    holder = await TitularRepo().get_by_id(session, usuario.id, titular_id)
    if holder is None:
        raise HTTPException(404, "não achei este titular")
    if holder.arquivado:
        return _holder(holder)
    accounts = await ContaCasaRepo().list_by_holder(session, usuario.id, titular_id)
    usages = await UsoContaCasaRepo().list_by_accounts(
        session, usuario.id, [account.id for account in accounts]
    )
    if any(account.estado == "EM_USO" for account in accounts) or any(
        usage.vigente_ate is None for usage in usages
    ):
        raise HTTPException(409, "troque a conta em uso antes de arquivar o titular")
    result = await TitularRepo().archive(session, usuario.id, titular_id)
    assert result is not None
    await session.commit()
    return _holder(result)


@router.get("/api/v1/titulares/{titular_id}/matriz", response_model=MatrizTitularSaida)
async def matriz_titular(
    titular_id: int,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
) -> MatrizTitularSaida:
    holder = await TitularRepo().get_by_id(session, usuario.id, titular_id)
    if holder is None:
        raise HTTPException(404, "não achei este titular")
    accounts = await ContaCasaRepo().list_by_holder(session, usuario.id, titular_id)
    return MatrizTitularSaida(
        titular=_holder(holder), contas=await _account_rows(session, usuario.id, accounts)
    )


@router.post(
    "/api/v1/titulares/{titular_id}/contas", response_model=ContaMatrizSaida, status_code=201
)
async def criar_conta_titular(
    titular_id: int,
    entrada: ContaEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ContaMatrizSaida:
    await session.execute(
        select(models.Usuario.id).where(models.Usuario.id == usuario.id).with_for_update()
    )
    holder = await TitularRepo().get_by_id(session, usuario.id, titular_id)
    if holder is None or holder.arquivado:
        raise HTTPException(404, "não achei este titular ativo")
    if await CasaRepo().get_nome_by_id(session, entrada.casa_id) is None:
        raise HTTPException(404, "não achei esta casa")
    try:
        account = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": usuario.id,
                "titular_id": titular_id,
                "casa_id": entrada.casa_id,
                "apelido": entrada.apelido,
                "estado": "DISPONIVEL",
                "ativa": True,
            },
        )
        rows = await _account_rows(
            session,
            usuario.id,
            await ContaCasaRepo().list_by_house(session, usuario.id, account.casa_id),
        )
        result = next(row for row in rows if row.conta_casa_id == account.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "já existe conta para este titular e casa ou apelido") from exc
    return result


@router.patch("/api/v1/titulares/{titular_id}/contas/{conta_id}", response_model=ContaMatrizSaida)
async def editar_conta_titular(
    titular_id: int,
    conta_id: int,
    entrada: ContaEdicao,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ContaMatrizSaida:
    await session.execute(
        select(models.Usuario.id).where(models.Usuario.id == usuario.id).with_for_update()
    )
    account = await ContaCasaRepo().get_by_id(session, usuario.id, conta_id)
    holder = await TitularRepo().get_by_id(session, usuario.id, titular_id)
    if account is None or account.titular_id != titular_id or holder is None or holder.arquivado:
        raise HTTPException(404, "não achei esta conta do titular ativo")
    if account.estado == "EM_USO":
        raise HTTPException(409, "troque a conta em uso antes de editá-la")
    try:
        changed = await ContaCasaRepo().update_details(
            session,
            usuario.id,
            conta_id,
            apelido=entrada.apelido or account.apelido,
            estado=entrada.estado or account.estado,
        )
        if changed is None:
            raise HTTPException(409, "a conta mudou durante a edição")
        rows = await _account_rows(
            session,
            usuario.id,
            await ContaCasaRepo().list_by_house(session, usuario.id, account.casa_id),
        )
        result = next(row for row in rows if row.conta_casa_id == conta_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "apelido já usado nesta casa") from exc
    return result


@router.post(
    "/api/v1/titulares/{titular_id}/contas/{conta_id}/ativar", response_model=ContaMatrizSaida
)
async def ativar_conta_titular(
    titular_id: int,
    conta_id: int,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ContaMatrizSaida:
    await session.execute(
        select(models.Usuario.id).where(models.Usuario.id == usuario.id).with_for_update()
    )
    holder = await TitularRepo().get_by_id(session, usuario.id, titular_id)
    account = await ContaCasaRepo().get_by_id(session, usuario.id, conta_id)
    if holder is None or holder.arquivado or account is None or account.titular_id != titular_id:
        raise HTTPException(404, "não achei esta conta do titular ativo")
    if (
        account.estado != "DISPONIVEL"
        or await UsoContaCasaRepo().current_for_update(session, usuario.id, account.casa_id)
        is not None
    ):
        raise HTTPException(409, "esta casa já tem conta em uso ou a conta não está disponível")
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    await UsoContaCasaRepo().open(session, usuario.id, account.casa_id, conta_id, now)
    await session.execute(
        update(models.ContaCasa)
        .where(
            models.ContaCasa.usuario_id == usuario.id,
            models.ContaCasa.id == conta_id,
        )
        .values(estado="EM_USO", ativa=True, desde=now, ate=None)
    )
    rows = await _account_rows(
        session,
        usuario.id,
        await ContaCasaRepo().list_by_house(session, usuario.id, account.casa_id),
    )
    result = next(row for row in rows if row.conta_casa_id == conta_id)
    await session.commit()
    return result
