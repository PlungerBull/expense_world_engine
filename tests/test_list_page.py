"""list_page — the shared count-plus-page shape.

Bloat audit 2026-08-06, Duplicates §12: eight list endpoints hand-rolled the
same count query + page query + ``LIMIT ${n+1} OFFSET ${n+2}`` placeholder
arithmetic, the last in two divergent idioms. These tests pin the single
implementation: count and page run off one predicate list, the window
arithmetic lands after every caller-appended param, and an empty condition
list is refused (the check that backs the tenant-predicate rule). Endpoint
envelopes remain pinned by the per-domain suites.
"""
import uuid

import pytest

from app import db
from app.helpers.pagination import list_page


@pytest.fixture
async def category_rows(test_data):
    """Three throwaway categories owned by the test user, one soft-deleted."""
    ids = [str(uuid.uuid4()) for _ in range(3)]
    async with db.pool.acquire() as conn:
        for i, cid in enumerate(ids):
            await conn.execute(
                """INSERT INTO expense_categories
                    (id, user_id, name, color, is_system, sort_order,
                     created_at, updated_at, deleted_at)
                   VALUES ($1, $2, $3, '#123456', false, $4,
                     now(), now(), CASE WHEN $5 THEN now() END)""",
                cid, test_data.user_id, f"list-page probe {i}", i, i == 2,
            )
    yield ids
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = ANY($1::uuid[])", ids
        )


@pytest.mark.asyncio
async def test_count_and_page_share_one_predicate(test_data, category_rows):
    async with db.pool.acquire() as conn:
        rows, total = await list_page(
            conn,
            from_sql="expense_categories",
            conditions=["user_id = $1", "deleted_at IS NULL", "name LIKE $2"],
            params=[test_data.user_id, "list-page probe %"],
            order_by="sort_order ASC",
            limit=50,
            offset=0,
        )
        # The soft-deleted third row is excluded from BOTH figures.
        assert total == 2
        assert [str(r["id"]) for r in rows] == category_rows[:2]


@pytest.mark.asyncio
async def test_window_lands_after_appended_params(test_data, category_rows):
    async with db.pool.acquire() as conn:
        # Two caller params + limit/offset: the $3/$4 window must not collide.
        rows, total = await list_page(
            conn,
            from_sql="expense_categories",
            conditions=["user_id = $1", "name LIKE $2"],
            params=[test_data.user_id, "list-page probe %"],
            order_by="sort_order ASC",
            limit=1,
            offset=1,
        )
        assert total == 3  # count ignores the window
        assert len(rows) == 1
        assert str(rows[0]["id"]) == category_rows[1]


@pytest.mark.asyncio
async def test_alias_and_select_projection(test_data, category_rows):
    async with db.pool.acquire() as conn:
        rows, total = await list_page(
            conn,
            from_sql="expense_categories c",
            conditions=["c.user_id = $1", "c.name LIKE $2"],
            params=[test_data.user_id, "list-page probe %"],
            order_by="c.sort_order ASC",
            limit=50,
            offset=0,
            select="c.id, c.name",
        )
        assert total == 3
        assert set(rows[0].keys()) == {"id", "name"}


@pytest.mark.asyncio
async def test_empty_conditions_refused(test_data):
    async with db.pool.acquire() as conn:
        with pytest.raises(ValueError):
            await list_page(
                conn,
                from_sql="expense_categories",
                conditions=[],
                params=[],
                order_by="created_at ASC",
                limit=50,
                offset=0,
            )
