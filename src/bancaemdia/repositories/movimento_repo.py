from datetime import date, datetime

from sqlalchemy import Date, and_, case, cast, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Movimento
from bancaemdia.domain.temporal import SaldoDaCasa
from bancaemdia.repositories.base import colunas


class MovimentoRepo:
    async def append(self, session: AsyncSession, dados: dict[str, object]) -> Movimento:
        obj = (
            await session.execute(
                insert(models.Movimento).values(**dados).returning(models.Movimento)
            )
        ).scalar_one()
        return Movimento(**colunas(obj))

    async def list_by_usuario(
        self,
        session: AsyncSession,
        usuario_id: int,
        desde: datetime | None = None,
        ate: datetime | None = None,
    ) -> list[Movimento]:
        stmt = select(models.Movimento).where(models.Movimento.usuario_id == usuario_id)
        if desde is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em >= desde)
        if ate is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em < ate)
        stmt = stmt.order_by(models.Movimento.ocorrido_em, models.Movimento.id)
        return [Movimento(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_by_conta_casa(
        self, session: AsyncSession, conta_casa_id: int
    ) -> list[Movimento]:
        stmt = (
            select(models.Movimento)
            .where(models.Movimento.conta_casa_id == conta_casa_id)
            .order_by(models.Movimento.ocorrido_em, models.Movimento.id)
        )
        return [Movimento(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def aggregate_saldos_by_usuario(
        self,
        session: AsyncSession,
        usuario_id: int,
        data_corte: date | None = None,
    ) -> dict[int, SaldoDaCasa]:
        """Resume o ledger no PostgreSQL e traz uma linha limitada por conta.

        A data civil é calculada em America/Sao_Paulo, igual ao domínio temporal. As CTEs
        evitam o produto cartesiano entre apostas e movimentos, que inflaria todas as somas.
        """

        movimento_dia = cast(func.timezone("America/Sao_Paulo", models.Movimento.ocorrido_em), Date)
        movimentos_stmt = select(
            models.Movimento.conta_casa_id.label("conta_casa_id"),
            func.coalesce(
                func.sum(
                    case(
                        (models.Movimento.tipo == "DEPOSITO", models.Movimento.valor_centavos),
                        else_=0,
                    )
                ),
                0,
            ).label("depositado_centavos"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            models.Movimento.tipo == "SAQUE",
                            func.abs(models.Movimento.valor_centavos),
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("sacado_centavos"),
            func.coalesce(
                func.sum(
                    case(
                        (models.Movimento.tipo == "BONUS", models.Movimento.valor_centavos),
                        else_=0,
                    )
                ),
                0,
            ).label("bonus_centavos"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            models.Movimento.tipo.notin_(("DEPOSITO", "SAQUE", "BONUS")),
                            models.Movimento.valor_centavos,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("movido_centavos"),
            func.count().label("movimentos"),
            func.min(movimento_dia).label("desde"),
        ).where(
            models.Movimento.usuario_id == usuario_id,
            models.Movimento.conta_casa_id.is_not(None),
        )
        if data_corte is not None:
            movimentos_stmt = movimentos_stmt.where(movimento_dia <= data_corte)
        movimentos = movimentos_stmt.group_by(models.Movimento.conta_casa_id).cte(
            "saldo_movimentos"
        )

        aposta_instante = func.coalesce(
            models.Aposta.data_aposta,
            models.Aposta.criada_em,
        )
        aposta_dia = cast(func.timezone("America/Sao_Paulo", aposta_instante), Date)
        aposta_antes_do_caixa = and_(
            movimentos.c.desde.is_not(None),
            aposta_dia < movimentos.c.desde,
        )
        apostas_stmt = (
            select(
                models.Aposta.conta_casa_id.label("conta_casa_id"),
                func.coalesce(func.sum(models.Aposta.stake_centavos), 0).label("apostado_centavos"),
                func.coalesce(func.sum(func.coalesce(models.Aposta.retorno_centavos, 0)), 0).label(
                    "retornado_centavos"
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (models.Aposta.estado == "PENDENTE", models.Aposta.stake_centavos),
                            else_=0,
                        )
                    ),
                    0,
                ).label("em_jogo_centavos"),
                func.count().filter(models.Aposta.estado == "PENDENTE").label("apostas_pendentes"),
                func.coalesce(
                    func.sum(
                        case(
                            (aposta_antes_do_caixa, 0),
                            else_=models.Aposta.stake_centavos,
                        )
                    ),
                    0,
                ).label("apostado_no_periodo_centavos"),
                func.coalesce(
                    func.sum(
                        case(
                            (aposta_antes_do_caixa, 0),
                            else_=func.coalesce(models.Aposta.retorno_centavos, 0),
                        )
                    ),
                    0,
                ).label("retornado_no_periodo_centavos"),
            )
            .select_from(models.Aposta)
            .outerjoin(
                movimentos,
                movimentos.c.conta_casa_id == models.Aposta.conta_casa_id,
            )
            .where(
                models.Aposta.usuario_id == usuario_id,
                models.Aposta.conta_casa_id.is_not(None),
                models.Aposta.selecionada.is_(True),
            )
        )
        if data_corte is not None:
            apostas_stmt = apostas_stmt.where(aposta_dia <= data_corte)
        apostas = apostas_stmt.group_by(
            models.Aposta.conta_casa_id,
            movimentos.c.desde,
        ).cte("saldo_apostas")

        stmt = (
            select(
                models.ContaCasa.id.label("conta_casa_id"),
                func.coalesce(movimentos.c.depositado_centavos, 0).label("depositado_centavos"),
                func.coalesce(movimentos.c.sacado_centavos, 0).label("sacado_centavos"),
                func.coalesce(movimentos.c.bonus_centavos, 0).label("bonus_centavos"),
                func.coalesce(movimentos.c.movido_centavos, 0).label("movido_centavos"),
                func.coalesce(apostas.c.apostado_centavos, 0).label("apostado_centavos"),
                func.coalesce(apostas.c.retornado_centavos, 0).label("retornado_centavos"),
                func.coalesce(apostas.c.em_jogo_centavos, 0).label("em_jogo_centavos"),
                func.coalesce(apostas.c.apostas_pendentes, 0).label("apostas_pendentes"),
                func.coalesce(movimentos.c.movimentos, 0).label("movimentos"),
                func.coalesce(apostas.c.apostado_no_periodo_centavos, 0).label(
                    "apostado_no_periodo_centavos"
                ),
                func.coalesce(apostas.c.retornado_no_periodo_centavos, 0).label(
                    "retornado_no_periodo_centavos"
                ),
                movimentos.c.desde,
            )
            .outerjoin(movimentos, movimentos.c.conta_casa_id == models.ContaCasa.id)
            .outerjoin(apostas, apostas.c.conta_casa_id == models.ContaCasa.id)
            .where(models.ContaCasa.usuario_id == usuario_id)
            .order_by(models.ContaCasa.id)
        )
        linhas = (await session.execute(stmt)).all()
        return {
            int(linha.conta_casa_id): SaldoDaCasa(
                conta_casa_id=int(linha.conta_casa_id),
                depositado_centavos=int(linha.depositado_centavos),
                sacado_centavos=int(linha.sacado_centavos),
                bonus_centavos=int(linha.bonus_centavos),
                movido_centavos=int(linha.movido_centavos),
                apostado_centavos=int(linha.apostado_centavos),
                retornado_centavos=int(linha.retornado_centavos),
                em_jogo_centavos=int(linha.em_jogo_centavos),
                apostas_pendentes=int(linha.apostas_pendentes),
                movimentos=int(linha.movimentos),
                apostado_no_periodo_centavos=int(linha.apostado_no_periodo_centavos),
                retornado_no_periodo_centavos=int(linha.retornado_no_periodo_centavos),
                desde=linha.desde,
            )
            for linha in linhas
        }

    async def list_page(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: dict[str, object],
        pagina: int = 1,
        tamanho: int = 50,
    ) -> tuple[list[Movimento], int]:
        stmt = select(models.Movimento, func.count().over().label("total")).where(
            models.Movimento.usuario_id == usuario_id
        )
        if filtros.get("conta_casa_id") is not None:
            stmt = stmt.where(models.Movimento.conta_casa_id == filtros["conta_casa_id"])
        if filtros.get("tipo") is not None:
            stmt = stmt.where(models.Movimento.tipo == filtros["tipo"])
        if filtros.get("desde") is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em >= filtros["desde"])
        if filtros.get("ate") is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em < filtros["ate"])
        stmt = (
            stmt
            .order_by(models.Movimento.ocorrido_em.desc(), models.Movimento.id.desc())
            .limit(tamanho)
            .offset((pagina - 1) * tamanho)
        )
        linhas = (await session.execute(stmt)).all()
        if linhas:
            return [Movimento(**colunas(linha[0])) for linha in linhas], int(linhas[0].total)
        if pagina == 1:
            return [], 0
        # Uma página depois do fim ainda precisa dizer quantas linhas existem; sem esta segunda
        # leitura a tela passaria a informar total zero só porque o deslocamento ficou grande.
        contagem = select(func.count()).select_from(
            stmt.limit(None).offset(None).order_by(None).subquery()
        )
        return [], int((await session.execute(contagem)).scalar_one())
