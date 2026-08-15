"""Reserved system-category names (bug 7.4).

A user category claiming @Opening used to succeed, after which every
``ensure_system_category`` seed for that key hit the LOWER(name) unique
index — which the INSERT's ON CONFLICT arbiter (system_key) does not cover —
and 500'd opening balances forever. @Opening is the only reserved name left:
@Debt and @Transfer went with the transfer removal (2026-08-10), which is why
the tests below read as one name rather than three. Two layers close it:

  * boundary: POST /categories, PUT rename, and POST /{id}/restore reject
    reserved names on non-system rows with 422 (system rows rename freely —
    lookup is by ``system_key``, and the spec guarantees renameability);
  * defense in depth: the seeding INSERT catches UniqueViolationError from a
    pre-fix squatter row and raises a clean 409 with the remedy.

Restore was added to that first layer on 2026-08-13 (bug 7.4-r). 7.4 shipped
with only the two caller-supplied-name paths, because restore takes the name off
the stored row and so did not look like a name-write at all — which is exactly
how it stayed open. All three paths are now pinned here.
"""
import uuid

import pytest

from app import db
from app.constants import SYSTEM_CATEGORY_DEFAULT_NAMES, SystemCategoryKey
from app.errors import AppError
from app.helpers.categories import ensure_system_category


def _idem():
    return {"X-Idempotency-Key": str(uuid.uuid4())}


@pytest.mark.asyncio
async def test_post_rejects_reserved_names_case_insensitively(client):
    """Every reserved name 422s on POST, in any casing (the index folds case)."""
    variants = [n for n in SYSTEM_CATEGORY_DEFAULT_NAMES.values()]
    variants += [n.lower() for n in SYSTEM_CATEGORY_DEFAULT_NAMES.values()]
    variants += [n.upper() for n in SYSTEM_CATEGORY_DEFAULT_NAMES.values()]
    for name in variants:
        r = await client.post(
            "/v1/categories",
            json={"id": str(uuid.uuid4()), "name": name, "color": "#112233"},
            headers=_idem(),
        )
        assert r.status_code == 422, (name, r.text)
        body = r.json()["error"]
        assert body["code"] == "VALIDATION_ERROR"
        assert "name" in (body.get("fields") or {}), (name, body)


@pytest.mark.asyncio
async def test_put_rejects_renaming_user_category_to_reserved_name(client, test_data):
    create = await client.post(
        "/v1/categories",
        json={"id": str(uuid.uuid4()), "name": f"plain-{uuid.uuid4()}", "color": "#112233"},
        headers=_idem(),
    )
    assert create.status_code == 201, create.text
    category_id = create.json()["id"]
    try:
        r = await client.put(
            f"/v1/categories/{category_id}",
            json={"name": "@opening"},
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert "name" in (r.json()["error"].get("fields") or {})
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM activity_log WHERE resource_id = $1", category_id)
            await conn.execute("DELETE FROM expense_categories WHERE id = $1", category_id)


@pytest.mark.asyncio
async def test_system_category_renames_freely_including_back_to_default(client, test_data):
    """System rows are exempt: rename away and back to the default both succeed."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            system_id = await ensure_system_category(
                conn, test_data.user_id, SystemCategoryKey.OPENING_BALANCE
            )

    away = await client.put(
        f"/v1/categories/{system_id}",
        json={"name": "Where tracking starts"},
        headers=_idem(),
    )
    assert away.status_code == 200, away.text

    back = await client.put(
        f"/v1/categories/{system_id}",
        json={"name": SYSTEM_CATEGORY_DEFAULT_NAMES[SystemCategoryKey.OPENING_BALANCE]},
        headers=_idem(),
    )
    assert back.status_code == 200, back.text
    assert back.json()["name"] == "@Opening"


@pytest.mark.asyncio
async def test_seeding_over_prefix_squatter_row_raises_409_not_500():
    """Defense in depth: a squatter row created before the boundary check
    shipped turns the seeding INSERT into a clean 409, not an uncaught
    UniqueViolationError. Uses a fresh user so the system row cannot already
    exist, and a lowercase squat to prove the LOWER(name) index is what fires.
    """
    squat_user_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO users (id, display_name, created_at, updated_at) VALUES ($1, 'squat-user', now(), now())",
                squat_user_id,
            )
            await conn.execute(
                """INSERT INTO expense_categories
                    (id, user_id, name, color, sort_order, created_at, updated_at)
                   VALUES ($1, $2, '@opening', '#111111', 1, now(), now())""",
                str(uuid.uuid4()), squat_user_id,
            )
            with pytest.raises(AppError) as exc_info:
                async with conn.transaction():
                    await ensure_system_category(
                        conn, squat_user_id, SystemCategoryKey.OPENING_BALANCE
                    )
            assert exc_info.value.status_code == 409
        finally:
            await conn.execute("DELETE FROM expense_categories WHERE user_id = $1", squat_user_id)
            await conn.execute("DELETE FROM users WHERE id = $1", squat_user_id)


@pytest.mark.asyncio
async def test_restore_rejects_a_soft_deleted_reserved_name(client, test_data):
    """The third write path (bug 7.4-r).

    7.4 sealed create and update, which are the two paths where the name is
    caller-supplied. Restore takes the name off the stored row, so it went
    unsealed: a user category that claimed '@Opening' before the guard shipped
    and was then soft-deleted could be restored straight back into the squat,
    re-breaking opening balances for that user.

    The squatter has to be hand-INSERTed — POST has rejected the name since 7.4,
    so the API can no longer produce the row this test needs. Soft-deleted at
    insert time for the same reason: DELETE would work, but building the state
    directly keeps the test about restore rather than about two other endpoints.
    """
    category_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO expense_categories
                    (id, user_id, name, color, sort_order,
                     created_at, updated_at, deleted_at)
                   VALUES ($1, $2, '@Opening', '#111111', 97,
                           now(), now(), now())""",
                category_id, test_data.user_id,
            )

            response = await client.post(
                f"/v1/categories/{category_id}/restore", headers=_idem()
            )

            assert response.status_code == 422, response.text
            body = response.json()["error"]
            assert body["code"] == "VALIDATION_ERROR"
            assert "name" in body["fields"]

            still_deleted = await conn.fetchval(
                "SELECT deleted_at IS NOT NULL FROM expense_categories WHERE id = $1",
                category_id,
            )
            assert still_deleted, "a refused restore must not clear deleted_at"
        finally:
            await conn.execute("DELETE FROM expense_categories WHERE id = $1", category_id)


@pytest.mark.asyncio
async def test_restore_of_an_ordinary_name_is_unaffected(client, test_data):
    """The guard must not turn every restore into a 422.

    Paired with the reserved case above so the new check is pinned as
    *selective*, not merely present — the failure mode of a badly-placed guard
    is that it fires on everything, and a lone negative test would not see it.
    """
    category_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO expense_categories
                    (id, user_id, name, color, sort_order,
                     created_at, updated_at, deleted_at)
                   VALUES ($1, $2, 'Groceries 7.4r', '#111111', 97,
                           now(), now(), now())""",
                category_id, test_data.user_id,
            )

            response = await client.post(
                f"/v1/categories/{category_id}/restore", headers=_idem()
            )
            assert response.status_code == 200, response.text
            assert response.json()["deleted_at"] is None
        finally:
            await conn.execute("DELETE FROM expense_categories WHERE id = $1", category_id)
