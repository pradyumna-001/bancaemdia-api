"""Global operator catalog and tenant-owned access confirmations."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class CatalogoOperador(Base):
    __tablename__ = "catalogo_operadores"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"), unique=True)


class CasaDominio(Base):
    __tablename__ = "casa_dominios"
    __table_args__ = (
        UniqueConstraint("marca", "hostname", name="uq_casa_dominio_marca_host"),
        CheckConstraint(
            "hostname = lower(hostname) AND hostname !~ '[*/:@?#]'", name="ck_catalogo_host_exato"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    marca: Mapped[str] = mapped_column(String(120))
    hostname: Mapped[str] = mapped_column(String(253))
    tecnico: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidencias: Mapped[dict[str, Any]] = mapped_column(JSONB)


class CatalogoFonte(Base):
    __tablename__ = "catalogo_fontes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), unique=True)
    jurisdiction: Mapped[str] = mapped_column(String(10))
    dados: Mapped[dict[str, Any]] = mapped_column(JSONB)


class CatalogoSnapshot(Base):
    __tablename__ = "catalogo_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_key", "snapshot_sha256", "consultado_em", name="uq_catalogo_snapshot_fonte_hash"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), ForeignKey("catalogo_fontes.source_key"))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    consultado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fonte: Mapped[dict[str, Any]] = mapped_column(JSONB)
    raw_base64: Mapped[str] = mapped_column(Text)


class CatalogoPublicacao(Base):
    __tablename__ = "catalogo_publicacoes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ambiente: Mapped[str] = mapped_column(String(40))
    emitido_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    etag: Mapped[str] = mapped_column(String(64), unique=True)


class CatalogoConfirmacao(Base):
    __tablename__ = "catalogo_confirmacoes"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "marca", "hostname", "evidence_sha256", name="uq_catalogo_confirmacao"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    marca: Mapped[str] = mapped_column(String(120))
    hostname: Mapped[str] = mapped_column(String(253))
    confirmado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_sha256: Mapped[str] = mapped_column(String(64))


class CatalogoAuditoria(Base):
    __tablename__ = "catalogo_auditoria"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    ocorrido_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acao: Mapped[str] = mapped_column(String(40))
    dados: Mapped[dict[str, Any]] = mapped_column(JSONB)
