import json
import uuid
from typing import Optional

import asyncpg


async def check_idempotency(
    conn: asyncpg.Connection,
    user_id: str,
    key: Optional[str],
) -> Optional[dict]:
    if key is None:
        return None
    row = await conn.fetchrow(
        """
        SELECT response_snapshot FROM idempotency_keys
        WHERE user_id = $1 AND key = $2 AND expires_at > now()
        """,
        user_id,
        key,
    )
    if row:
        return json.loads(row["response_snapshot"])
    return None


async def store_idempotency(
    conn: asyncpg.Connection,
    user_id: str,
    key: Optional[str],
    response: dict,
) -> None:
    if key is None:
        return
    await conn.execute(
        """
        INSERT INTO idempotency_keys (id, key, user_id, processed_at, response_snapshot, expires_at, created_at)
        VALUES ($1, $2, $3, now(), $4::jsonb, now() + interval '24 hours', now())
        ON CONFLICT (user_id, key) DO NOTHING
        """,
        str(uuid.uuid4()),
        key,
        user_id,
        json.dumps(response),
    )
