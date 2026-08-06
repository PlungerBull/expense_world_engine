"""Integration tests for the account archive/unarchive surface.

Accounts are the only archivable resource. Category and hashtag archiving
was deleted by sql/024 (docs/rework/WP5): archiving them was never distinct
from soft deleting them — `deleted_at` already hides a row from pickers
while leaving its past transactions intact. An archived ACCOUNT is
different: it still holds real money, so it keeps its flag, its two routes,
and its dashboard panel.

Run: .venv/bin/pytest tests/test_account_archive.py -v
"""
import uuid

import pytest

from app import db


async def _cleanup_account(account_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            account_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
            account_id, user_id,
        )


async def _activity_actions(resource_id: str, user_id: str) -> list[int]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action FROM activity_log
            WHERE resource_id = $1 AND user_id = $2
            ORDER BY created_at ASC
            """,
            resource_id, user_id,
        )
    return [r["action"] for r in rows]


@pytest.mark.asyncio
async def test_account_archive_unarchive_round_trip(client, test_data):
    """create → archive → unarchive: is_archived flips both ways and the
    activity log shows two UPDATED entries on top of CREATED."""
    account_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/accounts",
        json={
            "id": account_id,
            "name": f"archive-acct-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        archive_r = await client.post(
            f"/v1/accounts/{account_id}/archive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert archive_r.status_code == 200, archive_r.text
        assert archive_r.json()["is_archived"] is True

        unarchive_r = await client.post(
            f"/v1/accounts/{account_id}/unarchive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert unarchive_r.status_code == 200, unarchive_r.text
        body = unarchive_r.json()
        assert body["is_archived"] is False
        # CREATED + 2 UPDATED (archive + unarchive) = 3 mutations
        assert body["version"] >= 3

        # Activity actions: 1 (CREATED), 2 (UPDATED), 2 (UPDATED).
        assert await _activity_actions(account_id, test_data.user_id) == [1, 2, 2]
    finally:
        await _cleanup_account(account_id, test_data.user_id)


@pytest.mark.asyncio
async def test_account_unarchive_404_on_missing(client):
    r = await client.post(
        f"/v1/accounts/{uuid.uuid4()}/unarchive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_dashboard_default_omits_archived_accounts(client):
    """Without the flag, `archived_accounts` is present and null.

    There is exactly one archived panel. `archived_categories` and
    `archived_hashtags` were deleted with the read-time currency work
    (docs/rework/WP2), and sql/024 (WP5) then dropped `is_archived` from
    those two tables entirely.
    """
    r = await client.get("/v1/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archived_accounts"] is None
    assert "archived_categories" not in body
    assert "archived_hashtags" not in body


@pytest.mark.asyncio
async def test_dashboard_include_archived_returns_accounts_only(client, test_data):
    """The flag turns on `archived_accounts`, and nothing else."""
    account_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/accounts",
        json={
            "id": account_id,
            "name": f"archived-panel-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text
    archive_r = await client.post(
        f"/v1/accounts/{account_id}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert archive_r.status_code == 200, archive_r.text

    try:
        r = await client.get("/v1/dashboard?include_archived=true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["archived_accounts"] is not None
        assert isinstance(body["archived_accounts"], list)
        assert account_id in {a["id"] for a in body["archived_accounts"]}
        assert "archived_categories" not in body
        assert "archived_hashtags" not in body
    finally:
        await _cleanup_account(account_id, test_data.user_id)
