"""Compare PostgreSQL projections and ledger against append-only event evidence."""

import argparse
import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.cli.replay import ReplayInseguroError, ResultadoReplay, reconstruir_usuario
from bancaemdia.models import Banca
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.workers.materialization import get_engine

MIN_BANCAS_STAGING = 5
MIN_APOSTAS_STAGING = 16_000


def _divergiu(resultado: ResultadoReplay) -> bool:
    # Dry-run also reports projections and ledger rows that it *would* restore.
    return any((resultado.alteradas, resultado.recriadas, resultado.movimentos_restaurados))


@dataclass
class Conferencia:
    usuarios: int = 0
    bancas: int = 0
    apostas_reprocessadas: int = 0
    eventos: int = 0
    divergencias: int = 0
    erros: int = 0

    def passou(self, *, staging_gate: bool) -> bool:
        return (
            self.divergencias == 0
            and self.erros == 0
            and (
                not staging_gate
                or (
                    self.bancas >= MIN_BANCAS_STAGING
                    and self.apostas_reprocessadas >= MIN_APOSTAS_STAGING
                )
            )
        )


async def _contar_bancas(session: AsyncSession, usuario_id: int) -> int:
    # Banca has tenant RLS; count with the same tenant setting used by replay.
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(usuario_id)},
    )
    return int(
        await session.scalar(
            select(func.count()).select_from(Banca).where(Banca.usuario_id == usuario_id)
        )
        or 0
    )


async def _conferir_tudo() -> Conferencia:
    engine = get_engine()
    depois = 0
    resumo = Conferencia()
    while True:
        async with AsyncSession(engine) as session:
            ids = await UsuarioRepo().list_active_ids(session, depois, 1000)
        for usuario_id in ids:
            resumo.usuarios += 1
            try:
                resultado = await reconstruir_usuario(usuario_id, engine=engine, dry_run=True)
                async with AsyncSession(engine) as session:
                    resumo.bancas += await _contar_bancas(session, usuario_id)
            except ReplayInseguroError as erro:
                structlog.get_logger().error(
                    "conferencia_falhou", usuario_id=usuario_id, motivo=str(erro)
                )
                resumo.erros += 1
                continue
            structlog.get_logger().info("conferencia", **vars(resultado))
            resumo.apostas_reprocessadas += resultado.apostas
            resumo.eventos += resultado.eventos
            resumo.divergencias += int(_divergiu(resultado))
        if len(ids) < 1000:
            return resumo
        depois = ids[-1]


def _gravar_relatorio(path: Path, resumo: Conferencia, *, staging_gate: bool) -> None:
    report = {
        "source": "conferir_numeros.py",
        "environment": os.environ.get("APP_ENV", "unknown"),
        "checked_at": datetime.now(UTC).isoformat(),
        "mode": "staging-gate" if staging_gate else "all",
        "release_sha": os.environ.get("RELEASE_SHA"),
        **asdict(resumo),
        "passed": resumo.passou(staging_gate=staging_gate),
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    alvo = parser.add_mutually_exclusive_group(required=True)
    alvo.add_argument("--usuario-id", type=int)
    alvo.add_argument("--todos", action="store_true")
    parser.add_argument("--staging-gate", action="store_true", help="Require 5 banks and 16k bets")
    parser.add_argument("--json-out", type=Path, help="Write aggregate evidence without user IDs")
    args = parser.parse_args()
    if args.staging_gate and (not args.todos or os.environ.get("APP_ENV") != "staging"):
        parser.error("--staging-gate requires --todos and APP_ENV=staging")
    if args.staging_gate and not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("RELEASE_SHA", "")):
        parser.error("--staging-gate requires a 40-character RELEASE_SHA")
    if args.json_out and not args.todos:
        parser.error("--json-out requires --todos")
    if args.todos:
        resumo = asyncio.run(_conferir_tudo())
        if args.json_out:
            _gravar_relatorio(args.json_out, resumo, staging_gate=args.staging_gate)
        structlog.get_logger().info("conferencia_resumo", **asdict(resumo))
        return int(not resumo.passou(staging_gate=args.staging_gate))
    try:
        resultado = asyncio.run(reconstruir_usuario(args.usuario_id, dry_run=True))
    except ReplayInseguroError as erro:
        structlog.get_logger().error("conferencia_falhou", motivo=str(erro))
        return 1
    structlog.get_logger().info("conferencia", **vars(resultado))
    return int(_divergiu(resultado))


if __name__ == "__main__":
    raise SystemExit(main())
