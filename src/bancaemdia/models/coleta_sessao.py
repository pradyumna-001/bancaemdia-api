from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ColetaSessao(Base):
    __tablename__ = "coleta_sessoes"
    __table_args__ = (
        UniqueConstraint("id", "instalacao_id", "usuario_id", name="uq_coleta_sessao_identity"),
        ForeignKeyConstraint(
            ["instalacao_id", "usuario_id"],
            ["coleta_instalacoes.id", "coleta_instalacoes.usuario_id"],
            name="fk_coleta_sessao_installation",
        ),
        Index(
            "uq_coleta_sessao_open",
            "instalacao_id",
            unique=True,
            postgresql_where=text("encerrada_em IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sessao_id: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    instalacao_id: Mapped[int] = mapped_column(BigInteger)
    coletar_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    encerrada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ColetaEntrega(Base):
    """Durable submission ACK and worker inbox; raw data is never put on the broker."""

    __tablename__ = "coleta_entregas"
    __table_args__ = (
        UniqueConstraint("instalacao_id", "client_event_id", name="uq_coleta_entrega_event"),
        ForeignKeyConstraint(
            ["sessao_id", "instalacao_id", "usuario_id"],
            ["coleta_sessoes.id", "coleta_sessoes.instalacao_id", "coleta_sessoes.usuario_id"],
            name="fk_coleta_entrega_session",
        ),
        Index("ix_coleta_entregas_pending", "status", "id"),
        Index("ix_coleta_entregas_owner_day", "usuario_id", "criada_em"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    instalacao_id: Mapped[int] = mapped_column(BigInteger)
    sessao_id: Mapped[int] = mapped_column(BigInteger)
    batch_id: Mapped[UUID]
    client_event_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ack: Mapped[str] = mapped_column(String(16))
    ack_reason: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(40))
    aposta_chave: Mapped[str | None] = mapped_column(String)
    tentativas: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finalizada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
