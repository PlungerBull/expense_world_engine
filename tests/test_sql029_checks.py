"""Pins for sql/029 — the two closed-enum columns bug 6.3's sweep missed.

  * inbox_status_valid: status is 1 (pending) or 2 (promoted), nothing else.
    A dismissed row is status=1 + deleted_at, never a third value — the
    phantom `3 = dismissed` that stood in schema-reference.md was never
    written by any code path.
  * activity_log_action_valid: action is one of ActivityAction (1-4).

Both back the response models' IntEnum typing (bloat-audit §17f): a rogue
stored value fails loudly at the write now, so the enum-typed read can never
meet one.
"""
import uuid

import asyncpg
import pytest

from app import db


@pytest.mark.asyncio
async def test_inbox_status_pinned_to_1_and_2(test_data):
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO expense_transaction_inbox
                    (id, user_id, status, created_at, updated_at)
                   VALUES ($1, $2, 3, now(), now())""",
                str(uuid.uuid4()), test_data.user_id,
            )


@pytest.mark.asyncio
async def test_activity_action_pinned_to_enum(test_data):
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO activity_log
                    (id, user_id, resource_type, resource_id, action,
                     changed_by, created_at)
                   VALUES ($1, $2, 'transaction', $3, 5, $2, now())""",
                str(uuid.uuid4()), test_data.user_id, str(uuid.uuid4()),
            )
