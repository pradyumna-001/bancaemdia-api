from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from bancaemdia.core import context
from bancaemdia.middleware import rls


def _conexao():
    class Conexao:
        def __init__(self):
            self.executados = []

        def execute(self, statement, params):
            self.executados.append((statement.text, params))

    return Conexao()


def test_every_new_transaction_gets_the_request_user_again() -> None:
    conexao = _conexao()
    token = context.current_user_id.set("42")
    try:
        rls.apply_current_user(None, None, conexao)
    finally:
        context.current_user_id.reset(token)

    assert conexao.executados == [
        ("SELECT set_config('app.current_user_id', :uid, true)", {"uid": "42"})
    ]


def test_outside_a_request_the_transaction_is_left_alone() -> None:
    conexao = _conexao()

    rls.apply_current_user(None, None, conexao)

    assert conexao.executados == []


def test_the_hook_listens_to_every_session() -> None:
    assert event.contains(Session, "after_begin", rls.apply_current_user)
