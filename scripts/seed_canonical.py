import asyncio

import structlog

from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import engine


async def main() -> None:
    async with engine.begin() as conn:
        inserted = await seed_canonical(conn)
    await engine.dispose()
    structlog.get_logger().info("seed_canonical", **inserted)


if __name__ == "__main__":
    asyncio.run(main())
