"""Append-only operational journal, separate from financial domain projections."""

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, String, Table, func
from sqlalchemy.dialects.postgresql import JSONB

journal = Table(
    "reconciliacao_chunks",
    MetaData(),
    Column("usuario_id", BigInteger, primary_key=True),
    Column("report_sha256", String(64), primary_key=True),
    Column("chunk_index", Integer, primary_key=True),
    Column("next_offset", Integer, nullable=False),
    Column("batch_size", Integer, nullable=False),
    Column("audit", JSONB, nullable=False),
    Column("criado_em", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
