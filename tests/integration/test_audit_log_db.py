from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
async def test_audit_log_records_changes_without_values_and_rejects_mutation(banco) -> None:
    user_id = uuid4().int % (2**60)
    email = f"audit-{user_id}@example.test"
    engine = create_async_engine(banco.url_app)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(user_id)},
            )
            await conn.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'Before')"),
                {"id": user_id, "email": email},
            )
            await conn.execute(
                text("UPDATE usuarios SET nome = 'After' WHERE id = :id"), {"id": user_id}
            )
            rows = (
                await conn.execute(
                    text(
                        "SELECT action, resource_type, resource_id, diff FROM audit_log "
                        "WHERE usuario_id = :id ORDER BY id"
                    ),
                    {"id": user_id},
                )
            ).all()
            assert [(row.action, row.resource_type, row.resource_id) for row in rows] == [
                ("INSERT", "usuarios", str(user_id)),
                ("UPDATE", "usuarios", str(user_id)),
            ]
            assert rows[1].diff == {"fields": ["nome"]}
            assert email not in str(rows)

        with pytest.raises(DBAPIError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(user_id)},
                )
                await conn.execute(
                    text("DELETE FROM audit_log WHERE usuario_id = :id"), {"id": user_id}
                )
    finally:
        await engine.dispose()
