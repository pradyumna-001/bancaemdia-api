"""Read-only by default. Download public evidence; persist/publish only with --apply."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bancaemdia.coleta.catalogo import UFS, Source, Technical, sha256
from bancaemdia.coleta.catalogo_fontes import (
    JUDICIAL,
    NATIONAL,
    fetch_official,
    parse_html,
    parse_spreadsheet,
    spreadsheet_link,
)
from bancaemdia.models.casa_dominio import CatalogoFonte
from bancaemdia.services.catalogo import (
    audit,
    include_manual_candidates,
    publish,
    record_redirect,
    require_operator,
    synchronize,
    update_technical,
)
from bancaemdia.services.catalogo_assinatura import CatalogSigner, TrustRoot

ROOT = Path(__file__).resolve().parents[1]


def federal_snapshots(directory: Path) -> list[tuple[Source, bytes]]:
    now = datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True)
    captured = []
    with httpx.Client(timeout=30, trust_env=False) as client:
        national, national_chain = fetch_official(NATIONAL, {"www.gov.br"}, client)
        excel_url = spreadsheet_link(national, national_chain[-1])
        excel, excel_chain = fetch_official(excel_url, {"www.gov.br"}, client)
        judicial, judicial_chain = fetch_official(JUDICIAL, {"www.gov.br"}, client)
    for key, kind, raw, url, chain, parser, situation in (
        (
            "spa-national-html",
            "federal",
            national,
            NATIONAL,
            national_chain,
            parse_html,
            "autorizada",
        ),
        (
            "spa-national-xlsx",
            "federal",
            excel,
            excel_url,
            excel_chain,
            parse_spreadsheet,
            "autorizada",
        ),
        (
            "spa-judicial-html",
            "judicial",
            judicial,
            JUDICIAL,
            judicial_chain,
            parse_html,
            "judicial",
        ),
    ):
        unresolved: list[dict[str, str]] = []
        observations = parser(raw, situation, unresolved=unresolved)
        source = Source(
            source_key=key,
            kind=kind,
            jurisdiction="BR",
            url=url,
            consulted_at=now,
            valid_until=now + timedelta(days=7),
            snapshot_sha256=sha256(raw),
            observations=observations,
            unresolved_domains=unresolved,
        )
        path = directory / f"{now:%Y%m%dT%H%M%SZ}-{key}-{source.snapshot_sha256}.snapshot"
        if not path.exists():
            path.write_bytes(raw)
        metadata = {
            **source.model_dump(mode="json"),
            "redirect_chain": chain,
            "raw_file": path.name,
        }
        path.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        captured.append((source, raw))
    return captured


def source_matrix(path: Path) -> list[dict[str, Any]]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(matrix, list) or sorted(row["jurisdiction"] for row in matrix) != sorted(UFS):
        raise ValueError("source matrix must contain exactly 26 states and DF")
    for row in matrix:
        if row["status"] not in ("configured", "unconfigured", "unavailable"):
            raise ValueError("unknown source matrix status")
        from bancaemdia.coleta.catalogo import safe_url

        safe_url(row["url"])
        consulted = datetime.fromisoformat(row["consulted_at"])
        recheck = datetime.fromisoformat(row["recheck_at"])
        if consulted.utcoffset() is None or recheck.utcoffset() is None or recheck <= consulted:
            raise ValueError("dated matrix evidence and recheck time required")
        if not row.get("evidence"):
            raise ValueError("matrix requires evidence, not a fabricated assertion of no licenses")
    return matrix


async def run(args: argparse.Namespace) -> dict[str, Any]:
    captured = federal_snapshots(args.snapshots) if args.federal else []
    if args.source:
        source = Source.model_validate_json(args.source.read_text(encoding="utf-8"))
        if not args.raw:
            raise ValueError("--source requires --raw exact snapshot bytes")
        captured.append((source, args.raw.read_bytes()))
    matrix = source_matrix(args.matrix)
    if args.offline:
        if args.apply or args.publish or args.technical or args.manual or args.redirect_chain:
            raise ValueError("offline preview cannot persist or publish")
        return {
            "mode": "offline_empty_baseline_preview",
            "sources": [s.model_dump(mode="json") for s, _ in captured],
            "diff": {
                "added": sorted({o.key for s, _ in captured for o in s.observations}),
                "changed": [],
                "removed_from_source": [],
                "manual_unchanged": [],
            },
            "state_matrix": matrix,
            "totals": {
                "states": len(UFS),
                "unconfigured": sum(r["status"] != "configured" for r in matrix),
            },
        }
    if args.usuario_id is None:
        raise ValueError(
            "database operations require --usuario-id of an authorized catalog operator"
        )
    from bancaemdia.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(args.usuario_id)},
            )
            await require_operator(session)
            results = [
                await synchronize(session, args.usuario_id, s, raw, dry_run=not args.apply)
                for s, raw in captured
            ]
            for item in matrix:
                if not args.apply:
                    continue
                key = f"state-registry-{item['jurisdiction'].lower()}"
                row = await session.scalar(
                    select(CatalogoFonte).where(CatalogoFonte.source_key == key)
                )
                if row is None:
                    session.add(
                        CatalogoFonte(
                            source_key=key,
                            jurisdiction=item["jurisdiction"],
                            dados={"kind": "state_matrix", **item},
                        )
                    )
                else:
                    row.dados = {"kind": "state_matrix", **item}
            if args.apply:
                audit(
                    session,
                    args.usuario_id,
                    "source_matrix_sync",
                    {
                        "jurisdictions": len(matrix),
                        "matrix_sha256": sha256(args.matrix.read_bytes()),
                    },
                )
            manual = (
                await include_manual_candidates(session, args.usuario_id, dry_run=not args.apply)
                if args.manual
                else []
            )
            if args.technical:
                if not args.apply or not args.entry_id:
                    raise ValueError("technical update requires --apply --entry-id")
                await update_technical(
                    session,
                    args.usuario_id,
                    args.entry_id,
                    Technical.model_validate_json(args.technical.read_text(encoding="utf-8")),
                )
            published = None
            if args.redirect_chain:
                if not args.apply or not args.entry_id:
                    raise ValueError("redirect update requires --apply --entry-id")
                chain = json.loads(args.redirect_chain.read_text(encoding="utf-8"))
                await record_redirect(session, args.usuario_id, args.entry_id, chain)
            if args.publish:
                if not args.apply or not args.signing_key_file or not args.key_id:
                    raise ValueError("publication requires --apply and configured signing key/id")
                next_root = (
                    TrustRoot.model_validate_json(args.next_root.read_text(encoding="utf-8"))
                    if args.next_root
                    else None
                )
                signer = CatalogSigner.from_file(args.key_id, args.signing_key_file, next_root)
                published = (
                    await publish(
                        session,
                        args.usuario_id,
                        settings.APP_ENV,
                        signer,
                        minimum_client_version=args.minimum_client_version,
                    )
                ).id
            if args.apply:
                await session.commit()
            else:
                await session.rollback()
            return {
                "dry_run": not args.apply,
                "diffs": results,
                "manual_candidates": manual,
                "state_matrix": matrix,
                "catalog_version": published,
            }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usuario-id", type=int)
    parser.add_argument("--federal", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, default=ROOT / "docs/catalogo/fontes-estaduais.json")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--technical", type=Path)
    parser.add_argument("--entry-id", type=int)
    parser.add_argument("--redirect-chain", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--signing-key-file", type=Path)
    parser.add_argument("--key-id")
    parser.add_argument("--minimum-client-version", default="1.0.0")
    parser.add_argument("--next-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
