"""fetch_owned_row / fetch_owned_row_or_404 — the shared fetch-or-404 shape.

Bloat audit 2026-08-06, Duplicates §1: the tenant-isolation read predicate
(``id AND user_id AND deleted_at IS [NOT] NULL [FOR UPDATE]``) existed as ~28
independent literals. These tests pin the single implementation the sites now
share: tenant scoping, the deleted-flag polarity, the lock path, and the 404
translation. Endpoint-level behaviour (that every route still 404s a foreign
id) remains pinned by the per-domain suites.
"""
import uuid

import pytest

from app import db
from app.errors import AppError
from app.helpers.query_builder import fetch_owned_row, fetch_owned_row_or_404


@pytest.fixture
async def category_row(test_data):
    """A throwaway category owned by the test user."""
    category_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, is_system, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, 'fetch-owned-row probe', '#123456', false, 0,
                 now(), now())""",
            category_id, test_data.user_id,
        )
    yield category_id
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = $1", category_id
        )


@pytest.mark.asyncio
async def test_active_fetch_and_tenant_scoping(test_data, category_row):
    async with db.pool.acquire() as conn:
        row = await fetch_owned_row(
            conn, "expense_categories", category_row, test_data.user_id
        )
        assert row is not None
        assert str(row["id"]) == category_row

        # A different tenant sees nothing, even with the right id.
        foreign_user = str(uuid.uuid4())
        assert (
            await fetch_owned_row(
                conn, "expense_categories", category_row, foreign_user
            )
            is None
        )


@pytest.mark.asyncio
async def test_deleted_flag_polarity(test_data, category_row):
    async with db.pool.acquire() as conn:
        # Active row: deleted=True must refuse it.
        assert (
            await fetch_owned_row(
                conn, "expense_categories", category_row, test_data.user_id,
                deleted=True,
            )
            is None
        )

        await conn.execute(
            "UPDATE expense_categories SET deleted_at = now() WHERE id = $1",
            category_row,
        )

        # Soft-deleted row: the default (active) fetch must refuse it…
        assert (
            await fetch_owned_row(
                conn, "expense_categories", category_row, test_data.user_id
            )
            is None
        )
        # …and deleted=True resolves it.
        row = await fetch_owned_row(
            conn, "expense_categories", category_row, test_data.user_id,
            deleted=True,
        )
        assert row is not None
        assert row["deleted_at"] is not None


@pytest.mark.asyncio
async def test_for_update_returns_the_row(test_data, category_row):
    # FOR UPDATE requires an open transaction; lock contention itself is
    # exercised by test_concurrency_hazards.py at the endpoint level.
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            row = await fetch_owned_row(
                conn, "expense_categories", category_row, test_data.user_id,
                for_update=True,
            )
            assert row is not None


@pytest.mark.asyncio
async def test_or_404_raises_the_app_error_shape(test_data, category_row):
    async with db.pool.acquire() as conn:
        # Hit: returns the row, no raise.
        row = await fetch_owned_row_or_404(
            conn, "expense_categories", category_row, test_data.user_id,
            "category",
        )
        assert str(row["id"]) == category_row

        # Miss (foreign tenant): raises the standard 404.
        with pytest.raises(AppError) as exc_info:
            await fetch_owned_row_or_404(
                conn, "expense_categories", category_row, str(uuid.uuid4()),
                "category",
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "NOT_FOUND"
        assert exc_info.value.message == "category not found."
