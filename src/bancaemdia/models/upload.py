from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

# O estado do envio é contrato da API e sai na resposta: minúsculas, como a issue escreve.
ESTADOS_DO_UPLOAD = ("pending", "processing", "completed", "failed")
# O estado do bilhete é interno, como o das apostas: maiúsculas.
ESTADOS_DO_BILHETE = ("PENDENTE", "LIDO", "FALHOU", "IGNORADO", "TETO")


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(e) for e in ESTADOS_DO_UPLOAD)})",
            name="ck_uploads_status",
        ),
        Index("idx_uploads_usuario", "usuario_id", "criado_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # O identificador que sai para fora é sorteado, não o id da linha: ele viaja na URL de status
    # e no aviso do trabalhador, e um número em sequência contaria quantos envios existem.
    job_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), unique=True, server_default=func.gen_random_uuid()
    )
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    filename: Mapped[str] = mapped_column(String)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"))
    total_messages: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    estimated_bets: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    bets_processed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    bets_failed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    erro: Mapped[str | None] = mapped_column(String)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    concluido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadBilhete(Base):
    __tablename__ = "upload_bilhetes"
    __table_args__ = (
        UniqueConstraint("upload_id", "chat_id", "message_id", name="uq_upload_bilhetes_mensagem"),
        CheckConstraint(
            f"estado IN ({', '.join(repr(e) for e in ESTADOS_DO_BILHETE)})",
            name="ck_upload_bilhetes_estado",
        ),
        Index("idx_upload_bilhetes_usuario", "usuario_id", "criado_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    upload_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("uploads.id"))
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    midia_hash: Mapped[str | None] = mapped_column(String)
    estado: Mapped[str] = mapped_column(String, server_default=text("'PENDENTE'"))
    apostas: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    custo_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    enfileirado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UploadArquivo(Base):
    __tablename__ = "upload_arquivos"
    __table_args__ = (Index("idx_upload_arquivos_upload", "upload_id", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    upload_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("uploads.id"))
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    # O arquivo enviado espera aqui porque a API e os trabalhadores rodam em tarefas separadas do
    # ECS, sem disco em comum; a linha é apagada assim que o export é lido (o S3 é a issue #41).
    conteudo: Mapped[bytes] = mapped_column(LargeBinary)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
