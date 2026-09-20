from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Mensagem(Base):
    __tablename__ = "mensagens"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_mensagens_chat_message"),
        Index("idx_mensagens_data", "data"),
        Index("idx_mensagens_autor", "autor_bruto"),
        Index("idx_mensagens_midia_hash", "midia_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    autor_bruto: Mapped[str | None] = mapped_column(String)
    texto: Mapped[str] = mapped_column(String)
    data: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    versao_atual: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # A hora da edição decide qual export é mais novo: sem ela, reimportar um export antigo
    # devolveria o texto velho por cima do editado (regra do projeto antigo).
    editada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A foto pode chegar num export posterior ao da mensagem; o índice serve à releitura (#22).
    midia_hash: Mapped[str | None] = mapped_column(String)
