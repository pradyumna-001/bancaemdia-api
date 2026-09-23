"""Compare PostgreSQL projections and ledger against the append-only event evidence."""

import argparse
import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.cli.replay import ReplayInseguroError, reconstruir_usuario
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.workers.materialization import get_engine


async def _conferir_tudo() -> bool:
    engine = get_engine()
    depois = 0
    divergencias = False
    while True:
        async with AsyncSession(engine) as session:
            ids = await UsuarioRepo().list_active_ids(session, depois, 1000)
        for usuario_id in ids:
            try:
                resultado = await reconstruir_usuario(usuario_id, engine=engine, dry_run=True)
            except ReplayInseguroError as erro:
                structlog.get_logger().error(
                    "conferencia_falhou", usuario_id=usuario_id, motivo=str(erro)
                )
                divergencias = True
                continue
            structlog.get_logger().info("conferencia", **vars(resultado))
            divergencias |= resultado.alteradas != 0
        if len(ids) < 1000:
            return divergencias
        depois = ids[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    alvo = parser.add_mutually_exclusive_group(required=True)
    alvo.add_argument("--usuario-id", type=int)
    alvo.add_argument("--todos", action="store_true")
    args = parser.parse_args()
    if args.todos:
        return int(asyncio.run(_conferir_tudo()))
    try:
        resultado = asyncio.run(reconstruir_usuario(args.usuario_id, dry_run=True))
    except ReplayInseguroError as erro:
        structlog.get_logger().error("conferencia_falhou", motivo=str(erro))
        return 1
    structlog.get_logger().info("conferencia", **vars(resultado))
    return int(resultado.alteradas != 0)


if __name__ == "__main__":
    raise SystemExit(main())
