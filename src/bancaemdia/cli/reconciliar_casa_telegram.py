"""Reviewed historical backfill; all financial writes use the #110 domain service."""

import argparse
import asyncio
import hashlib
import importlib
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bancaemdia import models
from bancaemdia.cli.reconciliacao_journal import journal
from bancaemdia.cli.reconciliacao_report import (
    ALGORITHM,
    CLASSES,
    CONTRACT,
    MAX_BATCH,
    ReconciliationError,
    candidate_id,
    canonical,
    csv_view,
    digest,
    read_report,
    seal,
    summary,
    verify,
    write_report,
)
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.projecao import projetar

MAX_SOURCES = 20000
MAX_HISTORY = 1000
ZONE = ZoneInfo("America/Sao_Paulo")
# Changes anywhere in these inputs can change eligibility or a financial integrity check.
TENANT_TABLES = (
    "apostas",
    "eventos",
    "contas_casa",
    "unidades",
    "revisao_pendente",
    "aposta_consolidacoes",
    "cruzamento_entradas",
    "cruzamento_candidatos",
    "coletas_casa",
)
GLOBAL_TABLES = ("casas", "apelidos", "mercados", "competicoes", "esportes", "times", "tipsters")
METRICS = ("bet_count", "financial_fact_count", "stake", "return", "profit", "unresolved_value")


def runtime() -> SimpleNamespace:
    """Dependency is explicit; main alone must never silently fall back to legacy pairing."""
    try:
        engine = importlib.import_module("bancaemdia.domain.cruzamento")
        service = importlib.import_module("bancaemdia.domain.consolidacao_aposta")
        generator = importlib.import_module("bancaemdia.services.cruzamento_candidatos")
        accounts = importlib.import_module("bancaemdia.domain.account_attribution_service")
        repo = importlib.import_module("bancaemdia.repositories.cruzamento_candidato")
        relation = importlib.import_module("bancaemdia.models.aposta_consolidacao")
        pair = importlib.import_module("bancaemdia.models.cruzamento_candidato")
    except ImportError as error:
        raise ReconciliationError(
            "Pré-requisito ausente: integrar #110 / PR #166 e suas migrations"
        ) from error
    if engine.VERSION != ALGORITHM:
        raise ReconciliationError("Algoritmo instalado diverge do contrato da CLI")
    return SimpleNamespace(
        engine=engine,
        service=service,
        generator=generator,
        accounts=accounts,
        repo=repo.CruzamentoCandidatoRepo(),
        relation=relation.ApostaConsolidacao,
        pair=pair.CruzamentoCandidato,
    )


@dataclass(frozen=True)
class Filters:
    usuario_id: int
    desde: str | None = None
    ate: str | None = None
    casa: str | None = None
    algorithm_version: str = ALGORITHM

    def json(self) -> dict[str, Any]:
        return {
            "usuario_id": self.usuario_id,
            "desde": self.desde,
            "ate": self.ate,
            "casa": self.casa,
            "algorithm_version": self.algorithm_version,
        }

    def validate(self) -> None:
        if not 1 <= self.usuario_id <= 2**63 - 1 or self.algorithm_version != ALGORITHM:
            raise ReconciliationError("Usuário ou algoritmo inválido")
        lower, upper = boundary(self.desde), boundary(self.ate, upper=True)
        if lower and upper and lower >= upper:
            raise ReconciliationError("Intervalo inválido")
        if self.casa is not None and not self.casa:
            raise ReconciliationError("Casa inválida")


def boundary(value: str | None, *, upper: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZONE)
        if upper and len(value) == 10:
            instant += timedelta(days=1)
        return instant.astimezone(UTC)
    except ValueError as error:
        raise ReconciliationError("Data inválida; use ISO 8601") from error


def selected(source: dict[str, Any], filters: Filters) -> bool:
    if filters.casa and source["house"] != (casa_canonica(filters.casa) or filters.casa):
        return False
    lower, upper = boundary(filters.desde), boundary(filters.ate, upper=True)
    game = source["game"]
    # A missing game date is visible as an error when there is no date filter.
    return not ((lower or upper) and game is None) and (
        (lower is None or game >= lower) and (upper is None or game < upper)
    )


async def prepare(session: AsyncSession, user: int, *, writable: bool) -> None:
    rt = runtime()
    if await session.scalar(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
    ):
        raise ReconciliationError("Use papel operacional sem superuser/BYPASSRLS")
    if await session.scalar(text("SELECT pg_is_in_recovery()")):
        raise ReconciliationError("A reconciliação exige o primary")
    await session.execute(
        text("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(user)}
    )
    active = await session.scalar(select(models.Usuario.ativo).where(models.Usuario.id == user))
    if active is not True:
        raise ReconciliationError("Usuário inexistente ou inativo")
    if writable:
        # Same first lock as online; NOWAIT avoids lock inversion with pre-existing writers.
        await rt.repo.lock(session, user)
        names: tuple[str, ...] = (
            *TENANT_TABLES,
            *GLOBAL_TABLES,
            "usuarios",
            "reconciliacao_chunks",
        )
        if await session.scalar(text("SELECT to_regclass('public.usos_conta_casa')")):
            names = (*names, "usos_conta_casa")
        await session.execute(
            text(f"LOCK TABLE {', '.join(names)} IN SHARE ROW EXCLUSIVE MODE NOWAIT")
        )


async def watermark(session: AsyncSession, user: int) -> dict[str, Any]:
    checksum = hashlib.sha256()
    names = [(name, True) for name in TENANT_TABLES] + [(name, False) for name in GLOBAL_TABLES]
    if await session.scalar(text("SELECT to_regclass('public.usos_conta_casa')")):
        names.append(("usos_conta_casa", True))
    counts: dict[str, int] = {}
    for name, tenant in names:
        where = " WHERE t.usuario_id=:u" if tenant else ""
        # Names come exclusively from the constant allowlist, never operator input.
        order = "aposta_id" if name == "cruzamento_entradas" else "id"
        if name == "eventos":
            order = "id, criado_em"
        query = text(f"SELECT to_jsonb(t) AS row FROM {name} t{where} ORDER BY {order}")
        result = await session.stream(query, {"u": user})
        count = 0
        async for row in result:
            checksum.update(canonical([name, row[0]]) + b"\n")
            count += 1
        counts[name] = count
    usuario = await session.scalar(select(models.Usuario).where(models.Usuario.id == user))
    checksum.update(canonical(["usuario", user, usuario.ativo if usuario else None]))
    return {
        "sha256": checksum.hexdigest(),
        "counts": counts,
        "max_event_id": int(
            await session.scalar(
                select(func.max(models.Evento.id)).where(models.Evento.usuario_id == user)
            )
            or 0
        ),
        "max_bet_id": int(
            await session.scalar(
                select(func.max(models.Aposta.id)).where(models.Aposta.usuario_id == user)
            )
            or 0
        ),
    }


async def sources(session: AsyncSession, user: int, batch_size: int) -> list[dict[str, Any]]:
    rt = runtime()
    result: list[dict[str, Any]] = []
    after = 0
    account_rows = await session.execute(
        select(models.ContaCasa.id, models.Casa.nome)
        .join(models.Casa)
        .where(models.ContaCasa.usuario_id == user)
    )
    account_houses: dict[int, str] = dict(account_rows.tuples().all())
    while True:
        bets = list(
            await session.scalars(
                select(models.Aposta)
                .where(
                    models.Aposta.usuario_id == user,
                    models.Aposta.id > after,
                )
                .order_by(models.Aposta.id)
                .limit(batch_size)
                .execution_options(populate_existing=True)
            )
        )
        if not bets:
            break
        for bet in bets:
            history = (
                list(
                    await session.execute(
                        select(
                            models.Evento.tipo,
                            models.Evento.fonte,
                            models.Evento.payload_json,
                        )
                        .where(
                            models.Evento.usuario_id == user,
                            models.Evento.aposta_chave == bet.chave,
                        )
                        .order_by(models.Evento.id)
                        .limit(MAX_HISTORY + 1)
                    )
                )
                if bet.chave
                else []
            )
            error = None
            state: dict[str, Any] = {}
            if not history or history[0].tipo != "APOSTA_CRIADA" or len(history) > MAX_HISTORY:
                error = "incomplete_history"
            else:
                try:
                    state = projetar((kind, source, payload) for kind, source, payload in history)[
                        0
                    ]
                except (ValueError, KeyError, TypeError):
                    error = "invalid_history"
            raw = {key: state.get(key) for key in rt.generator.FIELDS}
            raw.update(
                usuario_id=user,
                stake_centavos=bet.stake_centavos,
                moeda="BRL",
                odd=bet.odd,
                estado=bet.estado,
                revisao_grave=bet.revisao_grave,
            )
            dictionary = await rt.generator.dictionary_for(session, raw)
            normalized = rt.engine.normalize(raw, dictionary)
            house = (
                casa_canonica(state.get("casa"))
                or account_houses.get(bet.conta_casa_id or 0)
                or "UNASSIGNED"
            )
            game = rt.accounts.game_instant(state)
            occurrence = rt.engine.instant(normalized["ocorrido_em"])
            if not normalized["casa"] or not occurrence:
                error = error or "unsearchable_source"
            result.append({
                "id": bet.id,
                "bet": bet,
                "state": state,
                "house": house,
                "game": game,
                "occurrence": occurrence,
                "error": error,
                "snapshot": {"original": raw, "normalized": normalized, "dictionary": dictionary},
            })
        after = bets[-1].id
        if len(result) > MAX_SOURCES:
            raise ReconciliationError(
                "Limite de 20000 fontes por usuário; ampliar orçamento exige ensaio de capacidade"
            )
    return result


def contribution(source: dict[str, Any]) -> dict[str, int]:
    bet = source["bet"]
    unresolved = bet.retorno_centavos is None or bet.estado == "PENDENTE" or bet.revisao_grave
    return {
        "financial_fact_count": 1,
        "stake": bet.stake_centavos,
        "return": bet.retorno_centavos or 0,
        "profit": 0 if bet.retorno_centavos is None else bet.retorno_centavos - bet.stake_centavos,
        "unresolved_value": bet.stake_centavos if unresolved else 0,
    }


def totals(items: list[dict[str, Any]], suppressed: set[int]) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    for source in items:
        group = groups.setdefault(source["house"], dict.fromkeys(METRICS, 0))
        group["bet_count"] += 1
        if source["bet"].selecionada and source["id"] not in suppressed:
            for key, value in contribution(source).items():
                group[key] += value
    return groups


async def relations(session: AsyncSession, user: int) -> list[Any]:
    cls = runtime().relation
    return list(await session.scalars(select(cls).where(cls.usuario_id == user).order_by(cls.id)))


def eligible(source: dict[str, Any], occupied: set[int]) -> bool:
    bet = source["bet"]
    return bool(
        bet.origem in {"casa", "telegram", "print"}
        and bet.selecionada
        and not bet.parceira_chave
        and not bet.duplicada_de
        and source["id"] not in occupied
        and not source["error"]
    )


async def report(
    session: AsyncSession, filters: Filters, batch_size: int, generated_at: str | None = None
) -> dict[str, Any]:
    rt = runtime()
    items = await sources(session, filters.usuario_id, batch_size)
    links = await relations(session, filters.usuario_id)
    active = [link for link in links if link.estado == "active"]
    occupied = {
        ident for link in active for ident in (link.casa_aposta_id, link.telegram_aposta_id)
    }
    suppressed = {link.telegram_aposta_id for link in active}
    declined = {
        (link.casa_aposta_id, link.telegram_aposta_id) for link in links if link.estado != "active"
    }
    by_id = {source["id"]: source for source in items}
    searchable = [source for source in items if eligible(source, occupied)]
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for source in searchable:
        groups[source["house"], source["bet"].origem == "casa"].append(source)
    for group in groups.values():
        group.sort(key=lambda source: (source["occurrence"], source["id"]))
    group_times = {key: [item["occurrence"] for item in group] for key, group in groups.items()}
    neighbors: dict[int, list[dict[str, Any]]] = {}
    saturated: set[int] = set()
    for source in searchable:
        opposite = groups[source["house"], source["bet"].origem != "casa"]
        times = group_times.get((source["house"], source["bet"].origem != "casa"), [])
        window = timedelta(days=rt.engine.WINDOW_DAYS)
        left = bisect_left(times, source["occurrence"] - window)
        right = bisect_right(times, source["occurrence"] + window)
        if right - left > rt.engine.MAX_NEIGHBORS:
            saturated.add(source["id"])
        neighbors[source["id"]] = opposite[left : min(right, left + rt.engine.MAX_NEIGHBORS)]
    # Hidden edges cannot evade the saturation veto, including when outside the filter.
    veto = set(saturated)
    for source in searchable:
        if any(item["id"] in saturated for item in neighbors[source["id"]]):
            veto.add(source["id"])
    edges: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
    degree: dict[int, int] = defaultdict(int)
    for house in searchable:
        if house["bet"].origem != "casa":
            continue
        for tip in neighbors[house["id"]]:
            verdict = rt.engine.compare(
                house["snapshot"]["normalized"], tip["snapshot"]["normalized"]
            )
            edges.append((house, tip, verdict))
            if verdict.classification in {"exact", "probable"}:
                degree[house["id"]] += 1
                degree[tip["id"]] += 1
    candidates: list[dict[str, Any]] = []
    projected_suppressed = set(suppressed)
    for house, tip, verdict in edges:
        if not selected(house, filters):
            continue
        classification, action, reason = verdict.classification, "none", "incompatible_evidence"
        if classification != "incompatible":
            blocked = house["id"] in veto or tip["id"] in veto
            competing = degree[house["id"]] > 1 or degree[tip["id"]] > 1
            if blocked or competing:
                classification, action = "competing", "review"
                reason = "incomplete_search" if blocked else "competing_sources"
            elif classification == "probable":
                action, reason = "review", "insufficient_evidence"
            else:
                action, reason = "consolidate", "exact_one_to_one"
                try:
                    resolution = await rt.accounts.account_for_state(
                        session, filters.usuario_id, house["state"], lock=False
                    )
                    if resolution.status != "UNIQUE":
                        action, reason = "review", "account_missing_or_ambiguous_at_game"
                except ValueError:
                    action, reason = "review", "invalid_explicit_account"
                hb, tb = house["bet"], tip["bet"]
                hs, ts = house["state"], tip["state"]
                if (hb.tipster_id and tb.tipster_id and hb.tipster_id != tb.tipster_id) or (
                    hs.get("tipster") and ts.get("tipster") and hs["tipster"] != ts["tipster"]
                ):
                    action, reason = "review", "context_conflict"
                if (house["id"], tip["id"]) in declined:
                    action, reason = "review", "previously_unlinked_or_rejected"
        if action == "consolidate":
            projected_suppressed.add(tip["id"])
        candidates.append({
            "candidate_id": candidate_id(filters.usuario_id, house["id"], tip["id"]),
            "house_id": house["id"],
            "telegram_id": tip["id"],
            "house": house["house"],
            "score": verdict.score,
            "classification": classification,
            "base_classification": verdict.classification,
            "matched": list(verdict.matched),
            "conflicts": list(verdict.conflicts),
            "missing": list(verdict.missing),
            "action": action,
            "reason": reason,
            "removed_contribution": contribution(tip),
        })
    for link in active:
        linked_house = by_id.get(link.casa_aposta_id)
        if linked_house and selected(linked_house, filters):
            candidates.append({
                "candidate_id": candidate_id(
                    filters.usuario_id, link.casa_aposta_id, link.telegram_aposta_id
                ),
                "house_id": link.casa_aposta_id,
                "telegram_id": link.telegram_aposta_id,
                "score": None,
                "classification": "already-consolidated",
                "matched": [],
                "conflicts": [],
                "action": "none",
                "reason": "active_relation",
                "relation_id": link.id,
            })
    for source in items:
        if (
            source["bet"].origem in {"casa", "telegram", "print"}
            and source["bet"].selecionada
            and not source["bet"].parceira_chave
            and not source["bet"].duplicada_de
            and source["error"]
            and source["id"] not in occupied
        ):
            hid, tid = (source["id"], 0) if source["bet"].origem == "casa" else (0, source["id"])
            candidates.append({
                "candidate_id": candidate_id(filters.usuario_id, hid, tid),
                "house_id": hid,
                "telegram_id": tid,
                "score": None,
                "classification": "error",
                "matched": [],
                "conflicts": [],
                "action": "none",
                "reason": source["error"],
            })
    candidates.sort(key=lambda item: (item["house_id"], item["telegram_id"]))
    return seal({
        "contract_version": CONTRACT,
        "algorithm_version": ALGORITHM,
        "filters": filters.json(),
        "batch_size": batch_size,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source_high_water_mark": await watermark(session, filters.usuario_id),
        "counts": {cls: sum(c["classification"] == cls for c in candidates) for cls in CLASSES},
        "candidates": candidates,
        "current_totals": totals(items, suppressed),
        "projected_totals": totals(items, projected_suppressed),
        "matching_index_sources": len(searchable),
    })


async def dry_run(engine: AsyncEngine, filters: Filters, batch_size: int = 100) -> dict[str, Any]:
    filters.validate()
    if not 1 <= batch_size <= MAX_BATCH:
        raise ReconciliationError("batch-size deve estar entre 1 e 200")
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await prepare(session, filters.usuario_id, writable=False)
        return await report(session, filters, batch_size)


def expected_totals(reviewed: dict[str, Any], offset: int) -> dict[str, dict[str, int]]:
    result = {house: values.copy() for house, values in reviewed["current_totals"].items()}
    for candidate in reviewed["candidates"][:offset]:
        if candidate["action"] == "consolidate":
            for key, value in candidate["removed_contribution"].items():
                result[candidate["house"]][key] -= value
    return result


async def observed_totals(
    session: AsyncSession, user: int, batch_size: int
) -> dict[str, dict[str, int]]:
    links = await relations(session, user)
    suppressed = {link.telegram_aposta_id for link in links if link.estado == "active"}
    return totals(await sources(session, user, batch_size), suppressed)


async def prepare_index(
    session: AsyncSession, user: int, batch_size: int
) -> dict[int, dict[str, Any]]:
    rt = runtime()
    items = await sources(session, user, batch_size)
    links = await relations(session, user)
    occupied = {
        ident
        for link in links
        if link.estado == "active"
        for ident in (link.casa_aposta_id, link.telegram_aposta_id)
    }
    for source in items:
        if eligible(source, occupied):
            await rt.repo.snapshot(
                session,
                {
                    "usuario_id": user,
                    "aposta_id": source["id"],
                    "casa": source["snapshot"]["normalized"]["casa"],
                    "origem": source["bet"].origem,
                    "ocorrido_em": source["occurrence"],
                    "dados": source["snapshot"],
                },
            )
    return {source["id"]: source for source in items}


async def apply_candidate(
    session: AsyncSession, user: int, candidate: dict[str, Any], by_id: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    rt = runtime()
    action = candidate["action"]
    if action not in {"consolidate", "review"}:
        return {"candidate_id": candidate["candidate_id"], "result": "no_action"}
    house, tip = by_id[candidate["house_id"]], by_id[candidate["telegram_id"]]
    for source in (house, tip):
        await rt.generator.generate(session, user, source["bet"], source["state"])
    pair = await session.scalar(
        select(rt.pair).where(
            rt.pair.usuario_id == user,
            rt.pair.casa_aposta_id == house["id"],
            rt.pair.telegram_aposta_id == tip["id"],
            rt.pair.versao == ALGORITHM,
        )
    )
    if pair is None:
        raise ReconciliationError("Candidato deixou de estar disponível")
    if action == "consolidate":
        if candidate["classification"] != "exact":
            raise ReconciliationError("Apenas exact aprovado pode consolidar")
        try:
            link = await rt.service.consolidate(
                session, user, house["id"], tip["id"], candidate_id=pair.id
            )
        except rt.service.ConsolidacaoRecusadaError as error:
            raise ReconciliationError(
                "Domínio recusou a decisão; gere e revise um novo relatório"
            ) from error
        return {
            "candidate_id": candidate["candidate_id"],
            "result": "consolidated",
            "relation_id": link.id,
        }
    # Shared generator owns probable/competing review; exact blocked by account/context needs one.
    if pair.revisao_id is None:
        review = models.RevisaoPendente(
            usuario_id=user,
            motivo="consolidacao_pendente",
            extracao_bruta={
                "candidato_id": pair.id,
                "versao": ALGORITHM,
                "aposta_chave": tip["bet"].chave,
                "parceira_suspeita": house["bet"].chave,
                "razao": candidate["reason"],
                "report_sha256_candidate": candidate["candidate_id"],
            },
        )
        session.add(review)
        await session.flush()
        pair.revisao_id = review.id
    else:
        existing_review = await session.get(models.RevisaoPendente, pair.revisao_id)
        if existing_review is None or existing_review.usuario_id != user:
            raise ReconciliationError("Revisão indisponível para este usuário")
        existing_review.resolvido_em = None
        existing_review.extracao_bruta = {
            **(existing_review.extracao_bruta or {}),
            "razao": candidate["reason"],
        }
    return {
        "candidate_id": candidate["candidate_id"],
        "result": "review",
        "review_id": pair.revisao_id,
    }


async def apply_report(
    engine: AsyncEngine,
    reviewed: dict[str, Any],
    approved_hash: str,
    filters: Filters,
    batch_size: int = 100,
    *,
    max_chunks: int | None = None,
) -> dict[str, Any]:
    verify(reviewed, approved_hash)
    filters.validate()
    if (
        filters.json() != reviewed.get("filters")
        or batch_size != reviewed.get("batch_size")
        or not 1 <= batch_size <= MAX_BATCH
    ):
        raise ReconciliationError("Filtros ou batch-size divergem do relatório aprovado")
    # A read-only inspection can report errors. An apply cannot assume incomplete history safe.
    if reviewed["counts"]["error"]:
        raise ReconciliationError(
            "Relatório contém fontes com erro; corrija antes de aprovar apply"
        )
    chunks = 0
    while True:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            await prepare(session, filters.usuario_id, writable=True)
            last = (
                (
                    await session.execute(
                        select(journal)
                        .where(
                            journal.c.usuario_id == filters.usuario_id,
                            journal.c.report_sha256 == approved_hash,
                        )
                        .order_by(journal.c.chunk_index.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            mark = await watermark(session, filters.usuario_id)
            if last is None:
                if mark != reviewed["source_high_water_mark"]:
                    raise ReconciliationError("Fontes alteradas desde o dry-run")
                if await report(session, filters, batch_size, reviewed["generated_at"]) != reviewed:
                    raise ReconciliationError("Plano diverge da avaliação atual")
                offset, chunk_index = 0, 0
            else:
                if (
                    last["batch_size"] != batch_size
                    or mark != last["audit"]["post_high_water_mark"]
                ):
                    raise ReconciliationError(
                        "Fontes alteradas desde o checkpoint; não é seguro retomar"
                    )
                offset, chunk_index = last["next_offset"], last["chunk_index"] + 1
            before = await observed_totals(session, filters.usuario_id, batch_size)
            if before != expected_totals(reviewed, offset):
                raise ReconciliationError(
                    f"Integridade anterior divergente; reinício no offset {offset}"
                )
            if last is not None and offset == len(reviewed["candidates"]):
                return {
                    "sha256": approved_hash,
                    "next_offset": offset,
                    "complete": True,
                    "idempotent": True,
                }
            end = min(offset + batch_size, len(reviewed["candidates"]))
            by_id = await prepare_index(session, filters.usuario_id, batch_size)
            actions = [
                await apply_candidate(session, filters.usuario_id, entry, by_id)
                for entry in reviewed["candidates"][offset:end]
            ]
            await session.flush()
            after = await observed_totals(session, filters.usuario_id, batch_size)
            if after != expected_totals(reviewed, end):
                raise ReconciliationError(
                    f"Integridade posterior divergente; chunk revertido, reinício no offset {offset}"
                )
            audit = {
                "contract_version": CONTRACT,
                "algorithm_version": ALGORITHM,
                "filters": filters.json(),
                "previous_chunk": None if last is None else digest(last["audit"]),
                "from_offset": offset,
                "to_offset": end,
                "pre_totals": before,
                "post_totals": after,
                "pre_high_water_mark": mark,
                "post_high_water_mark": await watermark(session, filters.usuario_id),
                "actions": actions,
            }
            await session.execute(
                insert(journal).values(
                    usuario_id=filters.usuario_id,
                    report_sha256=approved_hash,
                    chunk_index=chunk_index,
                    next_offset=end,
                    batch_size=batch_size,
                    audit=audit,
                )
            )
        # Checkpoint and domain changes committed together before any interruption point.
        chunks += 1
        complete = end == len(reviewed["candidates"])
        if complete or (max_chunks is not None and chunks >= max_chunks):
            return {
                "sha256": approved_hash,
                "next_offset": end,
                "complete": complete,
                "idempotent": False,
            }


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Reconciliação histórica Casa / Telegram; dry-run por padrão"
    )
    cli.add_argument("--usuario-id", type=int, required=True)
    cli.add_argument("--desde")
    cli.add_argument("--ate")
    cli.add_argument("--casa")
    cli.add_argument("--algorithm-version", default=ALGORITHM)
    cli.add_argument("--batch-size", type=int, default=100)
    cli.add_argument(
        "--report", type=Path, required=True, help="JSON a criar (dry-run) ou já revisado (apply)"
    )
    cli.add_argument("--csv", type=Path, help="Vista CSV adicional; nunca é autoridade de apply")
    cli.add_argument("--apply", action="store_true")
    cli.add_argument("--sha256", help="Hash explicitamente aprovado; obrigatório em apply")
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument(
        "--max-chunks",
        type=int,
        help="Parar após N chunks confirmados; retomar com o mesmo comando",
    )
    return cli


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from bancaemdia.config import get_settings

    if args.apply and (not args.sha256 or args.dry_run or args.csv):
        raise ReconciliationError("Apply exige --sha256 e não aceita --dry-run/--csv")
    if not args.apply and (args.sha256 or args.max_chunks):
        raise ReconciliationError("sha256/max-chunks são opções de apply")
    if args.max_chunks is not None and args.max_chunks < 1:
        raise ReconciliationError("max-chunks deve ser positivo")
    filters = Filters(args.usuario_id, args.desde, args.ate, args.casa, args.algorithm_version)
    runtime()
    engine = create_async_engine(get_settings().DATABASE_URL, pool_pre_ping=True)
    try:
        if args.apply:
            return await apply_report(
                engine,
                read_report(args.report),
                args.sha256,
                filters,
                args.batch_size,
                max_chunks=args.max_chunks,
            )
        result = await dry_run(engine, filters, args.batch_size)
        write_report(args.report, result)
        if args.csv:
            with args.csv.open("x", encoding="utf-8", newline="") as file:
                file.write(csv_view(result))
            args.csv.chmod(0o600)
        return result
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(run(args))
        sys.stdout.write(canonical(result).decode() + "\n" if args.apply else summary(result))
        return 0
    except ReconciliationError as error:
        sys.stderr.write(f"Reconciliação recusada: {error}\n")
        return 2
    except Exception:
        # Driver exceptions can contain URLs, raw statements or sensitive payloads.
        sys.stderr.write(
            "Falha operacional; chunk atual revertido. Verifique conexão, permissões e janela de manutenção.\n"
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
