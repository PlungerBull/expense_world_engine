"""New reference-data rows append: omitted sort_order = MAX + 1, not 0.

Bloat-audit 2026-08-06 Correctness §5 (deferred to the Duplicates §3 refactor,
landed 2026-08-08): all three creates used ``sort_order or 0``, so every new
row landed at 0 and CLAUDE.md's "new rows append (max+1 within the scope)"
was false — and an explicit ``sort_order: 0`` was indistinguishable from an
omitted one. Now `reference_data.next_sort_order` owns the append rule
(spanning soft-deleted rows, which keep their slot), and explicit values —
including 0 — are respected verbatim.
"""
import uuid

import pytest

from app import db
from app.helpers.reference_data import next_sort_order


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


async def _cleanup_hashtags(user_id: str, *ids: str) -> None:
    async with db.pool.acquire() as conn:
        for hashtag_id in ids:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                hashtag_id, user_id,
            )
            await conn.execute(
                "DELETE FROM expense_hashtags WHERE id = $1 AND user_id = $2",
                hashtag_id, user_id,
            )


@pytest.mark.asyncio
async def test_omitted_sort_order_appends(client, test_data):
    """Two creates without sort_order land on consecutive append slots."""
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        r = await client.post(
            "/v1/hashtags",
            json={"id": first_id, "name": f"append-a-{uuid.uuid4()}"},
            headers=_idem(),
        )
        assert r.status_code == 201, r.text
        first_slot = r.json()["sort_order"]

        r = await client.post(
            "/v1/hashtags",
            json={"id": second_id, "name": f"append-b-{uuid.uuid4()}"},
            headers=_idem(),
        )
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] == first_slot + 1
    finally:
        await _cleanup_hashtags(test_data.user_id, first_id, second_id)


@pytest.mark.asyncio
async def test_explicit_sort_order_zero_is_respected(client, test_data):
    """An explicit 0 stays 0 even when the collection is non-empty — the old
    `or 0` could not tell it apart from omitted (which now appends)."""
    anchor_id, explicit_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        r = await client.post(
            "/v1/hashtags",
            json={"id": anchor_id, "name": f"append-anchor-{uuid.uuid4()}"},
            headers=_idem(),
        )
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] > 0  # collection is non-empty (conftest seed)

        r = await client.post(
            "/v1/hashtags",
            json={
                "id": explicit_id,
                "name": f"append-explicit-{uuid.uuid4()}",
                "sort_order": 0,
            },
            headers=_idem(),
        )
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] == 0
    finally:
        await _cleanup_hashtags(test_data.user_id, anchor_id, explicit_id)


@pytest.mark.asyncio
async def test_next_sort_order_scoping_and_deleted_rows(test_data):
    """Direct helper checks: 0 on an empty collection (fresh user), per-user
    scoping, and soft-deleted rows keep their slot reserved."""
    user_b = str(uuid.uuid4())
    hashtag_b = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO users (id, display_name, created_at, updated_at)
                   VALUES ($1, 'sort-order-user-b', now(), now())""",
                user_b,
            )
            # Empty collection → the first slot is 0.
            assert await next_sort_order(conn, "expense_hashtags", user_b) == 0

            # A soft-deleted row at slot 7 keeps it reserved: next is 8.
            await conn.execute(
                """INSERT INTO expense_hashtags
                    (id, user_id, name, sort_order, created_at, updated_at, deleted_at)
                   VALUES ($1, $2, 'sort-order-deleted', 7, now(), now(), now())""",
                hashtag_b, user_b,
            )
            assert await next_sort_order(conn, "expense_hashtags", user_b) == 8

            # Another user's slots are invisible to this scope: bumping
            # user_b's row must not move the test user's append slot.
            own = await next_sort_order(conn, "expense_hashtags", test_data.user_id)
            await conn.execute(
                "UPDATE expense_hashtags SET sort_order = 99 WHERE id = $1", hashtag_b
            )
            assert (
                await next_sort_order(conn, "expense_hashtags", test_data.user_id)
                == own
            )
        finally:
            await conn.execute(
                "DELETE FROM expense_hashtags WHERE user_id = $1", user_b
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_b)
