from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    String,
    case,
    cast,
    func,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import LinhaExtrato


class ExtratoRepo:
    async def list_page(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: dict[str, object],
        pagina: int = 1,
        tamanho: int = 50,
    ) -> tuple[list[LinhaExtrato], int]:
        movimentos = select(
            literal("movimento", type_=String).label("origem"),
            models.Movimento.id.label("id"),
            models.Movimento.conta_casa_id.label("conta_casa_id"),
            models.Movimento.tipo.label("tipo"),
            models.Movimento.ocorrido_em.label("data_referencia"),
            literal("ocorrido_em", type_=String).label("data_referencia_origem"),
            models.Movimento.valor_centavos.label("valor_centavos"),
            models.Movimento.transferencia_id.label("transferencia_id"),
            models.Movimento.ocorrido_em.label("ocorrido_em"),
            models.Movimento.descricao.label("descricao"),
            cast(null(), String).label("chave"),
            cast(null(), String).label("estado"),
            cast(null(), BigInteger).label("stake_centavos"),
            cast(null(), BigInteger).label("retorno_centavos"),
            cast(null(), BigInteger).label("resultado_liquido_centavos"),
            cast(null(), Boolean).label("revisao_grave"),
        ).where(models.Movimento.usuario_id == usuario_id)

        referencia_aposta = func.coalesce(models.Aposta.data_aposta, models.Aposta.criada_em)
        apostas = select(
            literal("aposta", type_=String).label("origem"),
            models.Aposta.id.label("id"),
            models.Aposta.conta_casa_id.label("conta_casa_id"),
            literal("APOSTA_LIQUIDADA", type_=String).label("tipo"),
            referencia_aposta.label("data_referencia"),
            case(
                (models.Aposta.data_aposta.is_not(None), "data_aposta"),
                else_="criada_em",
            ).label("data_referencia_origem"),
            cast(null(), BigInteger).label("valor_centavos"),
            cast(null(), postgresql.UUID(as_uuid=True)).label("transferencia_id"),
            cast(null(), DateTime(timezone=True)).label("ocorrido_em"),
            cast(null(), String).label("descricao"),
            models.Aposta.chave.label("chave"),
            models.Aposta.estado.label("estado"),
            models.Aposta.stake_centavos.label("stake_centavos"),
            models.Aposta.retorno_centavos.label("retorno_centavos"),
            (models.Aposta.retorno_centavos - models.Aposta.stake_centavos).label(
                "resultado_liquido_centavos"
            ),
            models.Aposta.revisao_grave.label("revisao_grave"),
        ).where(
            models.Aposta.usuario_id == usuario_id,
            models.Aposta.selecionada.is_(True),
            models.Aposta.estado != "PENDENTE",
        )

        conta_casa_id = filtros.get("conta_casa_id")
        desde = filtros.get("desde")
        ate = filtros.get("ate")
        if conta_casa_id is not None:
            movimentos = movimentos.where(models.Movimento.conta_casa_id == conta_casa_id)
            apostas = apostas.where(models.Aposta.conta_casa_id == conta_casa_id)
        if desde is not None:
            movimentos = movimentos.where(models.Movimento.ocorrido_em >= desde)
            apostas = apostas.where(referencia_aposta >= desde)
        if ate is not None:
            movimentos = movimentos.where(models.Movimento.ocorrido_em < ate)
            apostas = apostas.where(referencia_aposta < ate)

        fontes = union_all(movimentos, apostas).subquery("extrato")
        stmt = (
            select(fontes, func.count().over().label("total"))
            .order_by(
                fontes.c.data_referencia.desc().nulls_last(),
                fontes.c.origem,
                fontes.c.id.desc(),
            )
            .limit(tamanho)
            .offset((pagina - 1) * tamanho)
        )
        linhas = (await session.execute(stmt)).all()
        if linhas:
            return [
                LinhaExtrato(
                    origem=linha.origem,
                    id=linha.id,
                    conta_casa_id=linha.conta_casa_id,
                    tipo=linha.tipo,
                    data_referencia=linha.data_referencia,
                    data_referencia_origem=linha.data_referencia_origem,
                    valor_centavos=linha.valor_centavos,
                    transferencia_id=linha.transferencia_id,
                    ocorrido_em=linha.ocorrido_em,
                    descricao=linha.descricao,
                    chave=linha.chave,
                    estado=linha.estado,
                    stake_centavos=linha.stake_centavos,
                    retorno_centavos=linha.retorno_centavos,
                    resultado_liquido_centavos=linha.resultado_liquido_centavos,
                    revisao_grave=linha.revisao_grave,
                )
                for linha in linhas
            ], int(linhas[0].total)
        if pagina == 1:
            return [], 0
        contagem = select(func.count()).select_from(fontes)
        return [], int((await session.execute(contagem)).scalar_one())
