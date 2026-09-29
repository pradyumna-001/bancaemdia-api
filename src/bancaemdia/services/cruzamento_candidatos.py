"""Transactional candidate generation; never changes bets, totals or financial events."""

from typing import Any

from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.conferencias import RE_CONFRONTO
from bancaemdia.domain.cruzamento import CONFIG, MAX_NEIGHBORS, VERSION, compare, instant, normalize
from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

outcomes = Counter(
    "cruzamento_candidates_total",
    "Matching assessments (not financial mutations)",
    ["version", "classification"],
)
FIELDS = (
    "casa",
    "identidade_bilhete",
    "ocorrido_em",
    "comeca_em",
    "evento",
    "competicao",
    "esporte",
    "mercado_bruto",
    "descricao",
    "tipo_aposta",
    "selecoes",
)


async def dictionary_for(session: AsyncSession, raw: dict[str, Any]) -> dict[str, str]:
    legs = raw.get("selecoes") or []
    events = {str(raw.get("evento") or ""), *(str(leg.get("evento") or "") for leg in legs[:32])}
    names = {
        "mercado": {
            str(raw.get("mercado_bruto") or ""),
            *(str(leg.get("mercado") or "") for leg in legs[:32]),
        },
        "competicao": {str(raw.get("competicao") or "")},
        "esporte": {str(raw.get("esporte") or "")},
        "time": {name for event in events for name in RE_CONFRONTO.split(event)},
    }
    dictionary: dict[str, str] = {}
    for kind, table in (
        ("mercado", models.Mercado),
        ("competicao", models.Competicao),
        ("esporte", models.Esporte),
        ("time", models.Time),
    ):
        wanted = sorted(names[kind] - {""})
        if not wanted:
            continue
        for ident, name in await session.execute(
            select(table.id, table.nome).where(table.nome.in_(wanted))
        ):
            dictionary[f"{kind}:{name}"] = f"{kind}#{ident}"
        for name, ident in await session.execute(
            select(models.Apelido.nome, models.Apelido.entidade_id).where(
                models.Apelido.entidade_tipo == kind,
                models.Apelido.nome.in_(wanted),
                models.Apelido.confirmado,
                models.Apelido.entidade_id.in_(select(table.id)),
            )
        ):
            dictionary.setdefault(f"{kind}:{name}", f"{kind}#{ident}")
    for event in events:
        parts = RE_CONFRONTO.split(event)
        if len(parts) == 2 and all(f"time:{p}" in dictionary for p in parts):
            # Home/away order is retained; handicap direction must not be inverted.
            dictionary[f"evento:{event}"] = " vs ".join(dictionary[f"time:{p}"] for p in parts)
    return dictionary


async def generate(session: AsyncSession, user: int, bet: Any, state: dict[str, Any]) -> str:
    if bet.usuario_id != user or bet.origem not in {"casa", "telegram", "print"}:
        return "nova"
    repo = CruzamentoCandidatoRepo()
    await repo.lock(session, user)
    bet = await session.scalar(
        select(models.Aposta)
        .where(
            models.Aposta.id == bet.id,
            models.Aposta.usuario_id == user,
        )
        .execution_options(populate_existing=True)
    )
    if bet is None:
        return "nova"
    await repo.invalidate(session, user, [bet.id])
    if not bet.selecionada or bet.parceira_chave or bet.duplicada_de:
        return "nova"
    raw = {key: state.get(key) for key in FIELDS}
    raw.update(
        usuario_id=user,
        stake_centavos=bet.stake_centavos,
        moeda="BRL",
        odd=bet.odd,
        estado=bet.estado,
        revisao_grave=bet.revisao_grave,
    )
    dictionary = await dictionary_for(session, raw)
    normalized = normalize(raw, dictionary)
    entry = await repo.snapshot(
        session,
        {
            "usuario_id": user,
            "aposta_id": bet.id,
            "casa": normalized["casa"],
            "origem": bet.origem,
            "ocorrido_em": instant(normalized["ocorrido_em"]),
            "dados": {"original": raw, "normalized": normalized, "dictionary": dictionary},
        },
    )
    if entry.casa is None or entry.ocorrido_em is None:
        outcomes.labels(version=VERSION, classification="unsearchable").inc()
        return "nova"
    neighbors = (await session.scalars(repo.neighbors_query(entry))).all()
    truncated = len(neighbors) > MAX_NEIGHBORS
    candidates = []
    for neighbor in neighbors[:MAX_NEIGHBORS]:
        house, tip = (entry, neighbor) if entry.origem == "casa" else (neighbor, entry)
        result = compare(house.dados["normalized"], tip.dados["normalized"])
        pair = await repo.upsert(
            session,
            {
                "usuario_id": user,
                "casa_aposta_id": house.aposta_id,
                "telegram_aposta_id": tip.aposta_id,
                "versao": VERSION,
                "score": result.score,
                "classe_base": result.classification,
                "status": result.classification,
                "busca_truncada": truncated,
                "sinais_iguais": list(result.matched),
                "sinais_conflitantes": list(result.conflicts),
                "evidencia": {
                    "casa": house.dados,
                    "telegram": tip.dados,
                    "config": CONFIG,
                    "veredicto": result.json(),
                },
                "explicacao": result.explanation,
            },
        )
        candidates.append(pair)
        outcomes.labels(version=VERSION, classification=result.classification).inc()
    await repo.adjudicate(session, user, candidates)
    statuses = {pair.status for pair in candidates}
    return "igual" if "exact" in statuses else ("duvida" if "probable" in statuses else "nova")


async def rebuild_page(session: AsyncSession, user: int, after: int = 0, limit: int = 100) -> int:
    from bancaemdia.domain.projecao import projetar

    if not 1 <= limit <= 200 or after < 0:
        raise ValueError("Invalid rebuild page")
    bets = (
        await session.scalars(
            select(models.Aposta)
            .where(
                models.Aposta.usuario_id == user,
                models.Aposta.id > after,
                models.Aposta.origem.in_(["casa", "telegram", "print"]),
            )
            .order_by(models.Aposta.id)
            .limit(limit)
        )
    ).all()
    for bet in bets:
        history = (
            await session.execute(
                select(models.Evento.tipo, models.Evento.fonte, models.Evento.payload_json)
                .where(
                    models.Evento.usuario_id == user,
                    models.Evento.aposta_chave == bet.chave,
                )
                .order_by(models.Evento.id)
                .limit(1001)
            )
        ).all()
        if len(history) > 1000:
            raise ValueError("Source history exceeds rebuild budget; explicit review required")
        state, _ = projetar([(kind, source, payload) for kind, source, payload in history])
        await generate(session, user, bet, state)
    return bets[-1].id if bets else after
