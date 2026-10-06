from contextvars import ContextVar

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
# Fora de um pedido HTTP (trabalhadores, comandos, testes que chamam get_db direto) não há roteador,
# e o primário nunca está atrasado.
use_primary: ContextVar[bool] = ContextVar("use_primary", default=True)
