from typing import Optional

import asyncpg

from app.config import settings

pool: Optional[asyncpg.Pool] = None


async def connect():
    global pool
    pool = await asyncpg.create_pool(settings.supabase_db_url)


async def disconnect():
    global pool
    if pool:
        await pool.close()
        pool = None
