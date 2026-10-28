from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ApostaLote(Base):
    __tablename__ = "apostas_lotes"
    __table_args__ = (
        UniqueConstraint("usuario_id", "chave", name="uq_apostas_lotes_chave"),
        UniqueConstraint("publico_id", name="uq_apostas_lotes_publico"),
        CheckConstraint(
            "(operacao = 'APAGAR' AND lote_id IS NULL AND chaves IS NOT NULL)"
            " OR (operacao = 'RESTAURAR' AND lote_id IS NOT NULL AND chaves IS NULL)",
            name="ck_apostas_lotes_operacao",
        ),
        Index(
            "uq_apostas_lotes_restauracao",
            "lote_id",
            unique=True,
            postgresql_where=text("operacao = 'RESTAURAR'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # O id do lote que o cliente vê; a restauração aponta para o do lote de exclusão.
    publico_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    operacao: Mapped[str] = mapped_column(String(16))
    lote_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    chave: Mapped[str] = mapped_column(String(200))
    pedido_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(SmallInteger)
    resposta: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # As chaves exatas que o lote apagou: a restauração devolve essas, não "tudo que está apagado".
    chaves: Mapped[list[str] | None] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
