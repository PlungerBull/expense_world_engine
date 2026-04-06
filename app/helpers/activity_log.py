import json
import uuid
from typing import Optional

import asyncpg


async def write_activity_log(
    conn: asyncpg.Connection,
    user_id: str,
    resource_type: str,
    resource_id: str,
    action: int,
    before_snapshot: Optional[dict] = None,
    after_snapshot: Optional[dict] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO activity_log (id, user_id, resource_type, resource_id, action,
                                  before_snapshot, after_snapshot, changed_by, created_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $2, now())
        """,
        str(uuid.uuid4()),
        user_id,
        resource_type,
        resource_id,
        action,
        json.dumps(before_snapshot) if before_snapshot else None,
        json.dumps(after_snapshot) if after_snapshot else None,
    )
