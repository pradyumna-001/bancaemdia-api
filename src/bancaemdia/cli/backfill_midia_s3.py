"""Copy legacy review photos to private S3 without deleting their database bytes."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.config import get_settings
from bancaemdia.db.session import engine
from bancaemdia.storage.midia_s3 import upload


async def backfill(*, batch_size: int = 100) -> int:
    bucket = get_settings().S3_UPLOAD_BUCKET
    if not bucket:
        raise RuntimeError("S3_UPLOAD_BUCKET is required")
    copied = 0
    while True:
        async with AsyncSession(engine) as session, session.begin():
            rows = (
                (
                    await session.execute(
                        select(models.MidiaArquivo)
                        .where(models.MidiaArquivo.s3_key.is_(None))
                        .order_by(models.MidiaArquivo.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return copied
            for row in rows:
                key = await upload(bucket, row.hash, row.conteudo)
                await session.execute(
                    update(models.MidiaArquivo)
                    .where(models.MidiaArquivo.id == row.id)
                    .values(s3_key=key)
                )
                copied += 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info("copied %s legacy images to S3", asyncio.run(backfill()))
