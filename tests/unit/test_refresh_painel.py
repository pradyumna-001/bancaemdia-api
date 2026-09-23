from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from bancaemdia.cli.refresh_painel import (
    MATERIALIZED_VIEWS,
    RefreshPainelEmAndamentoError,
    refresh_painel,
)


class _Transacao:
    def __init__(self, conexao: _Conexao) -> None:
        self.conexao = conexao

    async def __aenter__(self) -> None:
        self.conexao.em_transacao = True

    async def __aexit__(self, tipo: object, valor: object, traceback: object) -> None:
        if tipo is None:
            await self.conexao.commit()
        else:
            await self.conexao.rollback()


class _Conexao:
    def __init__(
        self,
        *,
        trava: bool = True,
        falhar_em: str | None = None,
    ) -> None:
        self.trava = trava
        self.falhar_em = falhar_em
        self.em_transacao = False
        self.comandos: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.atualizado_em = datetime(2026, 9, 21, 20, 30, tzinfo=UTC)

    async def scalar(self, comando: Any, parametros: object = None) -> object:
        sql = str(comando)
        self.comandos.append(sql)
        if "pg_try_advisory_lock" in sql:
            self.em_transacao = True
            return self.trava
        if "SELECT id FROM painel.estado_refresh" in sql:
            return 1
        if "UPDATE painel.estado_refresh" in sql:
            return self.atualizado_em
        raise AssertionError(sql)

    async def execute(self, comando: Any, parametros: object = None) -> None:
        sql = str(comando)
        self.comandos.append(sql)
        self.em_transacao = True
        if self.falhar_em is not None and self.falhar_em in sql:
            raise RuntimeError("refresh falhou")

    async def commit(self) -> None:
        self.commits += 1
        self.em_transacao = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.em_transacao = False

    def in_transaction(self) -> bool:
        return self.em_transacao

    def begin(self) -> _Transacao:
        return _Transacao(self)


class _ContextoConexao:
    def __init__(self, conexao: _Conexao) -> None:
        self.conexao = conexao

    async def __aenter__(self) -> _Conexao:
        return self.conexao

    async def __aexit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self, conexao: _Conexao) -> None:
        self.conexao = conexao

    def connect(self) -> _ContextoConexao:
        return _ContextoConexao(self.conexao)


@pytest.mark.asyncio
async def test_refresh_uses_one_snapshot_and_only_then_advances_the_clock() -> None:
    conexao = _Conexao()

    atualizado_em = await refresh_painel(_Engine(conexao))  # type: ignore[arg-type]

    assert atualizado_em == conexao.atualizado_em
    refreshes = [sql for sql in conexao.comandos if sql.startswith("REFRESH MATERIALIZED")]
    assert refreshes == [
        f"REFRESH MATERIALIZED VIEW CONCURRENTLY painel.{nome}" for nome in MATERIALIZED_VIEWS
    ]
    inicio = conexao.comandos.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    estado = next(
        indice
        for indice, sql in enumerate(conexao.comandos)
        if sql.startswith("UPDATE painel.estado_refresh")
    )
    assert inicio < min(conexao.comandos.index(sql) for sql in refreshes) < estado
    assert "SELECT pg_advisory_unlock(:lock_id)" == conexao.comandos[-1]
    assert conexao.commits == 3


@pytest.mark.asyncio
async def test_refresh_refuses_to_queue_behind_an_active_run() -> None:
    conexao = _Conexao(trava=False)

    with pytest.raises(RefreshPainelEmAndamentoError, match="já está sendo atualizado"):
        await refresh_painel(_Engine(conexao))  # type: ignore[arg-type]

    assert not any(sql.startswith("REFRESH MATERIALIZED") for sql in conexao.comandos)
    assert not any("pg_advisory_unlock" in sql for sql in conexao.comandos)


@pytest.mark.asyncio
async def test_failed_refresh_rolls_the_set_back_and_releases_the_lock() -> None:
    conexao = _Conexao(falhar_em="mv_painel_por_tipster")

    with pytest.raises(RuntimeError, match="refresh falhou"):
        await refresh_painel(_Engine(conexao))  # type: ignore[arg-type]

    assert conexao.rollbacks == 1
    assert not any(sql.startswith("UPDATE painel.estado_refresh") for sql in conexao.comandos)
    assert "SELECT pg_advisory_unlock(:lock_id)" == conexao.comandos[-1]
