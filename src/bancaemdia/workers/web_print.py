"""Durable DB inbox; the broker carries identifiers, never private image bytes."""

import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.domain.access import AccountReadOnlyError, require_write_access
from bancaemdia.domain.foto_aposta import validate_photo
from bancaemdia.domain.materializar import CupomLido, LeituraRecebida, apostas_da_leitura
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.extracao.modelos import ExtracaoBilhete
from bancaemdia.models.revisao_pendente import RevisaoPendente
from bancaemdia.models.web_print import PrintAsset
from bancaemdia.services.web_print import cipher, lock, now, owner, reserve_attempt
from bancaemdia.workers.celery_app import app
from bancaemdia.workers.extraction import RETRY_ON, extrair_bilhete
from bancaemdia.workers.materialization import _gravar, get_engine


def reading_domain(reading: dict[str, Any]) -> LeituraRecebida:
    def bilhete(value: Any) -> Any:
        return None if value is None else ExtracaoBilhete.model_validate(value).para_bilhete()

    return LeituraRecebida(
        bilhete=bilhete(reading.get("bilhete")),
        motivo=reading.get("motivo"),
        grave=bool(reading.get("grave")),
        nao_e_aposta=bool(reading.get("nao_e_aposta")),
        cupons=tuple(
            CupomLido(bilhete(c["bilhete"]), c.get("motivo"), bool(c.get("grave")))
            for c in reading.get("cupons", [])
        ),
    )


async def materialize(
    session: AsyncSession, uid: int, asset: PrintAsset, reading: dict[str, Any]
) -> list[dict[str, Any]]:
    # Only domain extraction fields enter public reviews, never provider cost.
    reading = {
        field: value
        for field, value in reading.items()
        if field in {"bilhete", "cupons", "motivo", "grave", "nao_e_aposta", "versao_prompt"}
    }
    if str(reading.get("motivo") or "").startswith("a leitura falhou"):
        reading["motivo"] = "não foi possível ler com segurança; confira a foto"
    novas = apostas_da_leitura(
        reading_domain(reading),
        chat_id=None,
        message_id=None,
        valor_unidade_centavos=VALOR_UNIDADE_PADRAO_CENTAVOS,
        print_id=asset.id,
    )
    results = []
    for nova in novas:
        bilhete = nova.bilhete
        # Unknown financial values stay null in the review. No zero-stake placeholder bet.
        if bilhete is None or bilhete.stake_unidades is None or bilhete.odd_total is None:
            review = RevisaoPendente(
                usuario_id=uid,
                midia_hash=asset.digest,
                motivo="print: dados financeiros incompletos",
                extracao_bruta={
                    "web_print_id": str(asset.id),
                    "ordem": nova.ordem,
                    "aposta_chave": nova.chave,
                    "campos": {
                        **nova.payload,
                        "stake_unidades": None if bilhete is None else bilhete.stake_unidades,
                    },
                    "extracao": reading,
                },
            )
            session.add(review)
            await session.flush()
            results.append({"type": "review", "bet_key": None, "review_ids": [review.id]})
            continue
        nova = replace(nova, payload={**nova.payload, "web_print_id": str(asset.id)})
        gravada = await _gravar(session, uid, nova, reading, asset.digest)
        ids = list(
            (
                await session.scalars(
                    select(RevisaoPendente.id).where(
                        RevisaoPendente.usuario_id == uid,
                        RevisaoPendente.extracao_bruta["aposta_chave"].astext == gravada.chave,
                        RevisaoPendente.resolvido_em.is_(None),
                    )
                )
            ).all()
        )
        results.append({
            "type": "review" if ids else "materialized",
            "bet_key": gravada.chave,
            "review_ids": ids,
        })
    return results


async def process_once(engine: AsyncEngine, uid: int, asset_id: UUID) -> bool:
    token = uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await owner(session, uid)
            asset = await session.scalar(
                select(PrintAsset)
                .where(PrintAsset.usuario_id == uid, PrintAsset.id == asset_id)
                .with_for_update(skip_locked=True)
            )
            if (
                asset is None
                or asset.status in {"completed", "failed"}
                or (
                    asset.status == "processing"
                    and asset.lease_until is not None
                    and asset.lease_until > now()
                )
            ):
                return False
            if asset.ciphertext is None or asset.media_expires_at <= now():
                asset.status, asset.error_code, asset.retryable = "failed", "media_expired", False
                return True
            if asset.status == "processing":
                if asset.attempt >= 3:
                    asset.status, asset.error_code, asset.retryable = "failed", "unavailable", False
                    return True
                await lock(session, uid)
                try:
                    await reserve_attempt(session, uid, asset)
                except HTTPException:
                    asset.status, asset.error_code, asset.retryable = "failed", "unavailable", True
                    return True
                asset.attempt += 1
                asset.quota_day = now()
            try:
                await require_write_access(session, uid)
            except AccountReadOnlyError:
                asset.status, asset.error_code, asset.retryable = (
                    "failed",
                    "account_read_only",
                    True,
                )
                return True
            asset.status, asset.lease_token, asset.lease_until = (
                "processing",
                token,
                now() + timedelta(minutes=10),
            )
            ciphertext, mime, digest, cached = (
                asset.ciphertext,
                asset.mime,
                asset.digest,
                asset.reading,
            )
    error = None
    retryable = False
    reading = cached
    try:
        if reading is None:
            content = cipher().decrypt(ciphertext)
            validate_photo(content, mime, digest)
            extension = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/gif": "gif",
                "image/webp": "webp",
            }[mime]
            reading = await asyncio.to_thread(
                extrair_bilhete,
                uid,
                base64.b64encode(content).decode("ascii"),
                nome_do_arquivo=f"foto.{extension}",
                private=True,
            )
            async with AsyncSession(engine) as saved_session:
                async with saved_session.begin():
                    await owner(saved_session, uid)
                    saved_asset = await saved_session.scalar(
                        select(PrintAsset)
                        .where(
                            PrintAsset.usuario_id == uid,
                            PrintAsset.id == asset_id,
                            PrintAsset.lease_token == token,
                        )
                        .with_for_update()
                    )
                    if saved_asset is None:
                        return False
                    saved_asset.reading = reading
        async with AsyncSession(engine, expire_on_commit=False) as session:
            async with session.begin():
                await owner(session, uid)
                asset = await session.scalar(
                    select(PrintAsset)
                    .where(
                        PrintAsset.usuario_id == uid,
                        PrintAsset.id == asset_id,
                        PrintAsset.lease_token == token,
                    )
                    .with_for_update()
                )
                if asset is None:
                    return False
                await require_write_access(session, uid)
                assert reading is not None
                asset.results = await materialize(session, uid, asset, reading)
                asset.reading, asset.status = reading, "completed"
                asset.error_code, asset.retryable, asset.lease_token, asset.lease_until = (
                    None,
                    False,
                    None,
                    None,
                )
        return True
    except AccountReadOnlyError:
        error, retryable = "account_read_only", True
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "P0402":
            raise
        error, retryable = "account_read_only", True
    except (*RETRY_ON, ConnectionError, TimeoutError):
        error, retryable = "unavailable", True
    except InvalidToken:
        error, retryable = "unavailable", True
    except (ValidationError, ValueError, TypeError, KeyError):
        error = "invalid_reading"
    async with AsyncSession(engine) as session:
        async with session.begin():
            await owner(session, uid)
            asset = await session.scalar(
                select(PrintAsset)
                .where(
                    PrintAsset.usuario_id == uid,
                    PrintAsset.id == asset_id,
                    PrintAsset.lease_token == token,
                )
                .with_for_update()
            )
            if asset is not None:
                asset.reading = reading
                asset.status, asset.error_code, asset.retryable = "failed", error, retryable
                asset.lease_token, asset.lease_until = None, None
    return True


def process_print(usuario_id: int, asset_id: str) -> bool:
    return asyncio.run(process_once(get_engine(), usuario_id, UUID(asset_id)))


async def dispatch(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT prints_purge()"))
        rows = (await conn.execute(text("SELECT * FROM prints_pending(100)"))).all()
    for uid, asset_id in rows:
        app.send_task(
            "materialization.web_print",
            kwargs={"usuario_id": uid, "asset_id": str(asset_id)},
            ignore_result=True,
        )


def tick() -> None:
    asyncio.run(dispatch(get_engine()))


process_print_task = app.task(name="materialization.web_print", ignore_result=True)(process_print)
tick_task = app.task(name="materialization.web_print_tick", ignore_result=True)(tick)
