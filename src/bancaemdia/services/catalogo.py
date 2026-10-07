"""Transactional catalog synchronization, publication and audited administration."""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.coleta.catalogo import (
    Observation,
    Source,
    Technical,
    canonical,
    normalize_redirects,
    sha256,
    source_diff,
    version,
)
from bancaemdia.models.casa_dominio import (
    CasaDominio,
    CatalogoAuditoria,
    CatalogoConfirmacao,
    CatalogoFonte,
    CatalogoPublicacao,
    CatalogoSnapshot,
)
from bancaemdia.services.catalogo_assinatura import CatalogSigner


async def require_operator(session: AsyncSession) -> None:
    if not await session.scalar(text("SELECT catalogo_is_operator()")):
        raise HTTPException(403, "Catalog operator authorization required")


async def lock_catalog(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(1132026)"))


def audit(session: AsyncSession, uid: int, action: str, data: dict[str, Any]) -> None:
    session.add(
        CatalogoAuditoria(usuario_id=uid, ocorrido_em=datetime.now(UTC), acao=action, dados=data)
    )


async def synchronize(
    session: AsyncSession,
    uid: int,
    source: Source,
    raw: bytes,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    await require_operator(session)
    if sha256(raw) != source.snapshot_sha256:
        raise ValueError("snapshot hash mismatch")
    await lock_catalog(session)
    previous = await session.scalar(
        select(CatalogoSnapshot)
        .where(
            CatalogoSnapshot.source_key == source.source_key,
        )
        .order_by(CatalogoSnapshot.id.desc())
        .limit(1)
    )
    old = (
        [Observation.model_validate(o) for o in previous.fonte["observations"]] if previous else []
    )
    manual = list((await session.scalars(select(CatalogoConfirmacao))).all())
    diff = source_diff(old, source, [f"{m.marca}|{m.hostname}" for m in manual])
    result: dict[str, Any] = {
        **diff,
        "source_key": source.source_key,
        "snapshot_sha256": source.snapshot_sha256,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    if previous and previous.snapshot_sha256 == source.snapshot_sha256:
        # Identical bytes cannot be reinterpreted by changing the submitted observations.
        if previous.fonte["observations"] != source.model_dump(mode="json")["observations"]:
            raise ValueError("identical snapshot cannot have different parsed observations")
        if previous.consultado_em == source.consulted_at:
            result["idempotent"] = True
            return result
    if previous and source.consulted_at < previous.consultado_em:
        raise ValueError("source consultation cannot move backwards")
    source_row = await session.scalar(
        select(CatalogoFonte).where(CatalogoFonte.source_key == source.source_key)
    )
    metadata = source.model_dump(mode="json", exclude={"observations"})
    metadata["status"] = "configured"
    if source_row is None:
        source_row = CatalogoFonte(
            source_key=source.source_key, jurisdiction=source.jurisdiction, dados=metadata
        )
        session.add(source_row)
        await session.flush()
    elif (
        source_row.jurisdiction != source.jurisdiction
        or source_row.dados.get("kind") != source.kind
    ):
        raise ValueError("source identity cannot change jurisdiction or class")
    else:
        source_row.dados = metadata
    snapshot = CatalogoSnapshot(
        source_key=source.source_key,
        snapshot_sha256=source.snapshot_sha256,
        consultado_em=source.consulted_at,
        fonte=source.model_dump(mode="json"),
        raw_base64=base64.b64encode(raw).decode(),
    )
    session.add(snapshot)
    await session.flush()
    observed = {o.key: o for o in source.observations}
    for key in sorted({o.key for o in old} | observed.keys()):
        brand, host = key.split("|", 1)
        row = await session.scalar(
            select(CasaDominio).where(CasaDominio.marca == brand, CasaDominio.hostname == host)
        )
        if row is None:
            row = CasaDominio(
                marca=brand, hostname=host, tecnico=Technical().model_dump(), evidencias={}
            )
            session.add(row)
        evidence = {
            **metadata,
            "snapshot_id": snapshot.id,
            "situation": observed[key].situation if key in observed else "removida_da_fonte",
            "company": observed[key].company
            if key in observed
            else row.evidencias.get(source.source_key, {}).get("company", ""),
        }
        row.evidencias = {**row.evidencias, source.source_key: evidence}
    audit(session, uid, "source_sync", result)
    await session.flush()
    return result


async def include_manual_candidates(
    session: AsyncSession, uid: int, *, dry_run: bool = True
) -> list[str]:
    await require_operator(session)
    await lock_catalog(session)
    keys = []
    for candidate in (
        await session.scalars(select(CatalogoConfirmacao).order_by(CatalogoConfirmacao.id))
    ).all():
        keys.append(f"{candidate.marca}|{candidate.hostname}")
        if dry_run:
            continue
        row = await session.scalar(
            select(CasaDominio).where(
                CasaDominio.marca == candidate.marca, CasaDominio.hostname == candidate.hostname
            )
        )
        if row is None:
            row = CasaDominio(
                marca=candidate.marca,
                hostname=candidate.hostname,
                tecnico=Technical().model_dump(),
                evidencias={},
            )
            session.add(row)
        row.evidencias = {
            **row.evidencias,
            f"manual-{candidate.id}": {
                "situation": "acesso_confirmado",
                "confirmed_at": candidate.confirmado_em.isoformat(),
                "evidence_sha256": candidate.evidence_sha256,
                "jurisdiction": "manual",
            },
        }
    if not dry_run:
        audit(session, uid, "manual_catalog_sync", {"candidates": len(keys)})
    await session.flush()
    return sorted(set(keys))


async def update_technical(
    session: AsyncSession, uid: int, entry_id: int, technical: Technical
) -> None:
    await require_operator(session)
    await lock_catalog(session)
    row = await session.get(CasaDominio, entry_id)
    if row is None:
        raise HTTPException(404, "Catalog entry not found")
    if technical.redirect_chain:
        from urllib.parse import urlsplit

        if urlsplit(technical.redirect_chain[-1]).hostname != row.hostname:
            raise ValueError("redirect chain must end at the catalog exact host")
    old = row.tecnico
    row.tecnico = technical.model_dump()
    audit(
        session, uid, "technical_changed", {"entry_id": row.id, "before": old, "after": row.tecnico}
    )


async def record_redirect(
    session: AsyncSession, uid: int, entry_id: int, chain: list[str]
) -> CasaDominio:
    """Reviewed redirect evidence never carries authorization/support to the new hostname."""
    await require_operator(session)
    await lock_catalog(session)
    old = await session.get(CasaDominio, entry_id)
    if old is None:
        raise HTTPException(404, "Catalog entry not found")
    final_host, aliases = normalize_redirects(chain)
    from urllib.parse import urlsplit

    if urlsplit(chain[0]).hostname != old.hostname or final_host == old.hostname:
        raise ValueError("redirect must start at this entry and end at a different exact host")
    target = await session.scalar(
        select(CasaDominio).where(
            CasaDominio.marca == old.marca, CasaDominio.hostname == final_host
        )
    )
    if target is None:
        target = CasaDominio(
            marca=old.marca, hostname=final_host, tecnico=Technical().model_dump(), evidencias={}
        )
        session.add(target)
        await session.flush()
    target_technical = Technical.model_validate(target.tecnico)
    target.tecnico = target_technical.model_copy(
        update={
            "aliases": sorted(set(target_technical.aliases) | set(aliases)),
            "redirect_chain": chain,
            "rollout": "disabled",
        }
    ).model_dump()
    old.tecnico = (
        Technical
        .model_validate(old.tecnico)
        .model_copy(update={"rollout": "disabled"})
        .model_dump()
    )
    proof = {
        "situation": "redirecionada",
        "chain": chain,
        "final_hostname": final_host,
        "observed_at": datetime.now(UTC).isoformat(),
        "chain_sha256": sha256(canonical(chain)),
    }
    old.evidencias = {**old.evidencias, "redirect-" + str(proof["chain_sha256"]): proof}
    audit(session, uid, "redirect_observed", {"from_entry": old.id, "to_entry": target.id, **proof})
    await session.flush()
    return target


async def publish(
    session: AsyncSession,
    uid: int,
    environment: str,
    signer: CatalogSigner,
    *,
    now: datetime | None = None,
    minimum_client_version: str = "1.0.0",
) -> CatalogoPublicacao:
    await require_operator(session)
    await lock_catalog(session)
    now = now or datetime.now(UTC)
    version(minimum_client_version)
    entries: list[dict[str, Any]] = [
        {
            "brand": row.marca,
            "hostname": row.hostname,
            **Technical.model_validate(row.tecnico).model_dump(),
        }
        for row in (
            await session.scalars(
                select(CasaDominio).order_by(CasaDominio.marca, CasaDominio.hostname)
            )
        ).all()
    ]
    # Reserve a durable monotonic sequence, then insert the complete immutable publication once.
    publication_id = await session.scalar(
        text("SELECT nextval(pg_get_serial_sequence('catalogo_publicacoes', 'id'))")
    )
    expires = now + timedelta(hours=1)
    payload: dict[str, object] = {
        "catalog_version": publication_id,
        "contract_version": 1,
        "environment": environment,
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "minimum_client_version": minimum_client_version,
        "last_known_good": {
            "maximum_age_seconds": 86400,
            "new_hosts_allowed": False,
            "revoked_hosts_allowed": False,
            "requires_previously_verified_signature": True,
        },
        "entries": entries,
    }
    envelope = signer.sign(payload)
    row = CatalogoPublicacao(
        id=publication_id,
        ambiente=environment,
        emitido_em=now,
        expira_em=expires,
        envelope=envelope,
        etag=sha256(canonical(envelope)),
    )
    session.add(row)
    audit(
        session,
        uid,
        "catalog_published",
        {"catalog_version": publication_id, "environment": environment, "etag": row.etag},
    )
    await session.flush()
    return row


async def admin_export(session: AsyncSession, uid: int) -> dict[str, Any]:
    await require_operator(session)
    entries: list[dict[str, Any]] = [
        {
            "id": r.id,
            "brand": r.marca,
            "hostname": r.hostname,
            "technical": r.tecnico,
            "evidence": r.evidencias,
        }
        for r in (await session.scalars(select(CasaDominio).order_by(CasaDominio.id))).all()
    ]
    sources = [
        {"source_key": r.source_key, "jurisdiction": r.jurisdiction, **r.dados}
        for r in (
            await session.scalars(select(CatalogoFonte).order_by(CatalogoFonte.source_key))
        ).all()
    ]
    from bancaemdia.coleta.catalogo import UFS

    configured = {s["jurisdiction"] for s in sources if s.get("status") == "configured"}
    result = {
        "entries": entries,
        "sources": sources,
        "observed_authorizations": {
            kind: sorted({
                f"{entry['brand']}|{entry['hostname']}"
                for entry in entries
                for evidence in entry["evidence"].values()
                if evidence.get("kind") == kind
                and evidence.get("situation") == ("autorizada" if kind == "federal" else "judicial")
                and datetime.fromisoformat(evidence["valid_until"]) > datetime.now(UTC)
            })
            for kind in ("federal", "judicial")
        },
        "totals": {
            "entries": len(entries),
            "state_jurisdictions": len(UFS),
            "configured_states": len(configured & set(UFS)),
            "unconfigured_states": sorted(set(UFS) - configured),
        },
    }
    audit(session, uid, "catalog_export", {"entries": len(entries), "sources": len(sources)})
    await session.flush()
    return result
