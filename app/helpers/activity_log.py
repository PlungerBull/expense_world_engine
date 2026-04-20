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
    actor_type: str = "user",
    actor_id: Optional[str] = None,
) -> None:
    """Write an immutable audit row for a mutation.

    ``actor_type`` / ``actor_id`` separate who performed the mutation
    from who owns the resource. Defaults mirror the pre-separation
    behaviour: ``actor_type='user'`` and ``actor_id=user_id``. System
    jobs (cron refreshes, admin back-office) should pass
    ``actor_type='system'`` / ``'admin'`` so downstream audit queries
    can filter by origin without guessing.
    """
    await conn.execute(
        """
        INSERT INTO activity_log (id, user_id, resource_type, resource_id, action,
                                  before_snapshot, after_snapshot, changed_by,
                                  actor_type, created_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, now())
        """,
        str(uuid.uuid4()),
        user_id,
        resource_type,
        resource_id,
        action,
        json.dumps(before_snapshot) if before_snapshot else None,
        json.dumps(after_snapshot) if after_snapshot else None,
        actor_id or user_id,
        actor_type,
    )
