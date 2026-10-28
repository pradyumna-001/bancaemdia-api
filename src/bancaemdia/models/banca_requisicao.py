from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class BancaRequisicao(Base):
    __tablename__ = "bancas_requisicoes"
    __table_args__ = (
        UniqueConstraint("usuario_id", "chave", name="uq_bancas_requisicoes_chave"),
        CheckConstraint(
            "operacao IN ('CRIAR','ALTERAR','ARQUIVAR')", name="ck_bancas_requisicoes_operacao"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    operacao: Mapped[str] = mapped_column(String(16))
    banca_id: Mapped[int | None] = mapped_column(BigInteger)
    chave: Mapped[str] = mapped_column(String(200))
    pedido_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(SmallInteger)
    resposta: Mapped[dict[str, object]] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
