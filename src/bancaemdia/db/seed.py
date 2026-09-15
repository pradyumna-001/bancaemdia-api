from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from bancaemdia.db.models import Base
from bancaemdia.domain import vocabulario
from bancaemdia.models import Casa, Competicao, Esporte, Mercado, Time


async def seed_canonical(conn: AsyncConnection) -> dict[str, int]:
    inserted = {
        "casas": await _insert(
            conn, Casa, [{"nome": nome, "dominio": dominio} for nome, dominio in vocabulario.CASAS]
        ),
        "esportes": await _insert(
            conn, Esporte, [{"nome": nome, "icone": icone} for nome, icone in vocabulario.ESPORTES]
        ),
    }
    esportes = dict((await conn.execute(select(Esporte.nome, Esporte.id))).tuples().all())
    inserted["competicoes"] = await _insert(
        conn,
        Competicao,
        [
            {"nome": nome, "esporte_id": esportes[esporte], "pais": pais}
            for nome, esporte, pais in vocabulario.COMPETICOES
        ],
    )
    inserted["mercados"] = await _insert(
        conn,
        Mercado,
        [{"nome": nome, "familia": familia} for nome, familia in vocabulario.MERCADOS],
    )
    inserted["times"] = await _insert(
        conn,
        Time,
        [{"nome": nome, "apelidos": list(apelidos)} for nome, apelidos in vocabulario.TIMES],
    )
    return inserted


async def _insert(conn: AsyncConnection, model: type[Base], rows: list[dict[str, object]]) -> int:
    result = await conn.execute(insert(model).values(rows).on_conflict_do_nothing())
    return result.rowcount
