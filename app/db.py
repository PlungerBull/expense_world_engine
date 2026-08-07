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
        # pgBouncer transaction mode hands a different backend connection to
        # each transaction, so client-side prepared statement caches reference
        # OIDs that may not exist on the next checkout. Disabling the cache
        # makes us pgBouncer-safe; cost is negligible since round-trip latency
        # dominates parse cost for our query sizes.
        statement_cache_size=0,
        # A statement that outlives this is a hung request, not a slow one —
        # nothing the engine serves takes seconds, let alone thirty.
        command_timeout=settings.db_command_timeout,
    )


async def disconnect() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None
