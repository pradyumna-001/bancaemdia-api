"""Rebuild a bounded tenant page from source events; prints only a resumable cursor."""

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.services.cruzamento_candidatos import rebuild_page
from bancaemdia.workers.materialization import get_engine


async def main(user: int, after: int, limit: int) -> int:
    async with AsyncSession(get_engine(), expire_on_commit=False) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user)}
        )
        return await rebuild_page(session, user, after, limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usuario", type=int, required=True)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print(asyncio.run(main(args.usuario, args.after, args.limit)))  # ruff: ignore[print]
