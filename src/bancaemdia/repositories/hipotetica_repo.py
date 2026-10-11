"""Owned, bounded full population and separately resolved confirmed evidence."""

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bancaemdia import models
from bancaemdia.domain.financeiro import Estado
from bancaemdia.domain.hipotetica import Dica, Fato, comparar
from bancaemdia.repositories.aposta_consolidacao import financial_predicate
from bancaemdia.repositories.selecao_apostas import selecionar_apostas

MAX_APOSTAS = 10_000


def _comissao_especial(value: str | None) -> bool:
    try:
        number = Decimal("0" if value is None else value)
        return not number.is_finite() or number != 0
    except InvalidOperation:
        return True


class HipoteticaRepo:
    async def validar_ids(
        self, session: AsyncSession, owner: int, filtros: dict[str, object]
    ) -> None:
        for campo, entity, privado in (
            ("conta_casa_id", models.ContaCasa, True),
            ("titular_id", models.Titular, True),
            ("banca_id", models.Banca, True),
            ("grupo_id", models.GrupoAposta, True),
            ("casa_id", models.Casa, False),
            ("tipster_id", models.Tipster, False),
            ("mercado_id", models.Mercado, False),
            ("competicao_id", models.Competicao, False),
        ):
            if filtros.get(campo) is None:
                continue
            model: Any = entity
            query = select(model.id).where(model.id == filtros[campo])
            if privado:
                query = query.where(model.usuario_id == owner)
            if await session.scalar(query) is None:
                raise HTTPException(404, "filtro_nao_encontrado")

    async def consultar(
        self, session: AsyncSession, owner: int, filtros: dict[str, object]
    ) -> dict[str, object]:
        await self.validar_ids(session, owner, filtros)
        a = models.Aposta
        query = (
            selecionar_apostas(owner, filtros)
            .with_only_columns(
                *a.__table__.columns,
                cast(a.odd, String).label("odd_texto"),
                financial_predicate().label("financeiro"),
            )
            .order_by(a.id)
            .limit(MAX_APOSTAS + 1)
        )
        rows = list((await session.execute(query)).mappings())
        if len(rows) > MAX_APOSTAS:
            raise HTTPException(422, "populacao_excede_10000_apostas_reduza_os_filtros")
        casa_ids = [r["id"] for r in rows if r["origem"] == "casa"]
        vinculos: dict[int, list[Dica | None]] = {}
        metadata: dict[str, Any] = {}
        if casa_ids:
            rel = models.ApostaConsolidacao
            tip = aliased(a)
            # Tip is evidence, not a second population row: no origin/date/visibility
            # filter from the real selection is propagated to this lookup.
            linked = await session.execute(
                select(
                    rel.casa_aposta_id, tip.id, tip.stake_centavos, tip.revisao_grave, tip.freebet
                )
                .outerjoin(
                    tip,
                    (tip.id == rel.telegram_aposta_id)
                    & (tip.usuario_id == owner)
                    & (tip.origem.in_(("telegram", "telegram_bot", "print"))),
                )
                .where(
                    rel.usuario_id == owner,
                    rel.estado == "active",
                    rel.casa_aposta_id.in_(casa_ids),
                )
            )
            for house_id, tip_id, stake, review, freebet in linked:
                vinculos.setdefault(house_id, []).append(
                    None if tip_id is None else Dica(stake, review, freebet)
                )
            keys = [r["chave"] for r in rows if r["id"] in vinculos and r["chave"] is not None]
            if keys:
                e = models.Evento
                # Correlated indexed probes return only flags/latest commission;
                # event histories and JSON payloads are never returned or loaded.
                origin = select(a.chave).where(a.usuario_id == owner, a.chave.in_(keys)).subquery()
                key = origin.c.chave
                owned = (e.usuario_id == owner) & (e.aposta_chave == key)
                history = select(e.id).where(owned, e.tipo == "APOSTA_CRIADA").exists()
                commission = (
                    select(e.payload_json["comissao_centavos"].astext)
                    .where(owned, e.payload_json.has_key("comissao_centavos"))
                    .order_by(e.id.desc(), e.criado_em.desc())
                    .limit(1)
                    .scalar_subquery()
                )
                special = (
                    select(e.id)
                    .where(
                        owned,
                        (
                            (e.fonte.in_(("manual", "planilha")))
                            & e.payload_json.has_key("retorno_centavos")
                        )
                        | e.payload_json.has_key("bonus_centavos")
                        | e.payload_json.has_key("taxa_centavos")
                        | e.payload_json.has_key("retorno_fixo_centavos"),
                    )
                    .exists()
                )
                result = await session.execute(
                    select(
                        key,
                        history.label("historico"),
                        commission.label("comissao"),
                        special.label("especial"),
                    )
                )
                metadata = {r["chave"]: r for r in result.mappings()}
        fatos = []
        for r in rows:
            meta = metadata.get(r["chave"], {})
            fatos.append(
                Fato(
                    id=r["id"],
                    origem=r["origem"],
                    estado=Estado(r["estado"]),
                    custo=r["stake_centavos"],
                    face=r["valor_aposta_centavos"] if r["freebet"] else r["stake_centavos"],
                    retorno=r["retorno_centavos"],
                    odd=r["odd_texto"],
                    freebet=r["freebet"],
                    revisao=r["revisao_grave"],
                    financeiro=r["financeiro"],
                    duvida=r["duvida_de_par"],
                    historico=bool(meta.get("historico")),
                    especial=bool(meta.get("especial")) or _comissao_especial(meta.get("comissao")),
                )
            )
        return comparar(fatos, vinculos)
