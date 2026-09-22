from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class MidiaArquivo(Base):
    __tablename__ = "midia_arquivos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Guardada por impressão digital, como no projeto antigo (`dados/midia/<hash>`): o mesmo print
    # mandado por dez pessoas ocupa espaço uma vez. Sai daqui para o S3 na issue #41.
    hash: Mapped[str] = mapped_column(String, ForeignKey("midias.hash"), unique=True)
    conteudo: Mapped[bytes] = mapped_column(LargeBinary)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
