"""Installation credentials and disposable pairing challenges; never plaintext secrets."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ColetaInstalacao(Base):
    __tablename__ = "coleta_instalacoes"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "instalacao_publica_id", name="uq_coleta_instalacao_owner_public"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    instalacao_publica_id: Mapped[UUID]
    nome_dispositivo: Mapped[str | None] = mapped_column(String(80))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_prefixo: Mapped[str | None] = mapped_column(String(12))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    pareado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultimo_uso_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotacionado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revogado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expira_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_legado_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)


class ColetaPairingCode(Base):
    __tablename__ = "coleta_pairing_codes"
    __table_args__ = (
        CheckConstraint(
            "expira_em > criado_em AND expira_em <= criado_em + interval '30 minutes'",
            name="ck_pairing_code_lifetime",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ColetaPairingQuota(Base):
    __tablename__ = "coleta_pairing_quotas"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    used: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
