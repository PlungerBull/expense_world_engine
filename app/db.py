from typing import Optional

import asyncpg

from app.config import settings

pool: Optional[asyncpg.Pool] = None


async def connect() -> None:
    global pool
    pool = await asyncpg.create_pool(
        settings.supabase_db_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )


async def disconnect() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None
