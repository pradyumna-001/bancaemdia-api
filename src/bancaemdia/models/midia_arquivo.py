from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class MidiaArquivo(Base):
    __tablename__ = "midia_arquivos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Guardada por impressão digital: o mesmo print ocupa espaço uma vez.
    hash: Mapped[str] = mapped_column(String, ForeignKey("midias.hash"), unique=True)
    conteudo: Mapped[bytes] = mapped_column(LargeBinary)
    s3_key: Mapped[str | None] = mapped_column(String)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
