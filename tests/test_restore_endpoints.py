"""Regression tests for the per-resource restore endpoints.

The transaction restore path has its own dedicated test file
(`test_transaction_restore.py`) because its inverse logic is intricate
(balance re-apply, junction precision, reconciliation handling).
The four endpoints covered here are the
"simpler" restores — accounts, categories, hashtags and reconciliations —
but each carries its own resource-specific guard rail that this file
pins down:

  * Accounts: round-trip (clear deleted_at, RESTORED activity entry).
  * Categories: same, plus a name-collision check that returns 409 when
    an active category has taken over the deleted one's display name.
  * Hashtags: same name-collision check, AND the deliberate decision
    NOT to re-link cascaded junction rows (restoring would silently
    re-tag transactions the user may no longer want labeled).
  * Reconciliations: round-trip (transactions unassigned during the
    delete are NOT re-linked on restore — same reasoning as hashtags).
  * Inbox: the exception — there is NO restore route (owner decision
    2026-08-14). A draft is not a financial record; dismissing it is
    final. The tests at the end of this file pin the route's absence
    and the fact that the delete is still soft.

Run: .venv/bin/pytest tests/test_restore_endpoints.py -v
"""
import uuid

import pytest

from app import db


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


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


async def _cleanup_category(category_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            category_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = $1 AND user_id = $2",
            category_id, user_id,
        )


async def _cleanup_hashtag(hashtag_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE hashtag_id = $1 AND user_id = $2",
            hashtag_id, user_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            hashtag_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_hashtags WHERE id = $1 AND user_id = $2",
            hashtag_id, user_id,
        )


async def _cleanup_reconciliation(recon_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_transactions SET reconciliation_id = NULL WHERE reconciliation_id = $1",
            recon_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            recon_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_reconciliations WHERE id = $1 AND user_id = $2",
            recon_id, user_id,
        )


async def _cleanup_inbox(inbox_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            inbox_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2",
            inbox_id, user_id,
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


# ---------------------------------------------------------------------------
# Account restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_account_round_trip(client, test_data):
    """Create → delete → restore an empty account.

    After restore: deleted_at cleared, version > 1, activity log shows
    CREATED → DELETED → RESTORED.
    """
    account_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/accounts",
        json={
            "id": account_id,
            "name": f"restore-acct-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        delete_r = await client.delete(
            f"/v1/accounts/{account_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert delete_r.status_code == 200, delete_r.text
        assert delete_r.json()["deleted_at"] is not None

        restore_r = await client.post(
            f"/v1/accounts/{account_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        body = restore_r.json()
        assert body["deleted_at"] is None
        assert body["version"] >= 3  # 1 create + 1 delete + 1 restore = 3 mutations

        assert await _activity_actions(account_id, test_data.user_id) == [1, 3, 4]

    finally:
        await _cleanup_account(account_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Category restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_category_round_trip(client, test_data):
    """Create → delete → restore a category. Verify activity trail."""
    category_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/categories",
        json={
            "id": category_id,
            "name": f"restore-cat-{uuid.uuid4()}",
            "color": "#abc123",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        del_r = await client.delete(
            f"/v1/categories/{category_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert del_r.status_code == 200, del_r.text

        restore_r = await client.post(
            f"/v1/categories/{category_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        assert restore_r.json()["deleted_at"] is None

        assert await _activity_actions(category_id, test_data.user_id) == [1, 3, 4]

    finally:
        await _cleanup_category(category_id, test_data.user_id)


# The name-collision branches in restore_category / restore_hashtag are LIVE:
# sql/012 replaced the full UNIQUE (user_id, name) constraints with partial
# unique indexes WHERE deleted_at IS NULL, so a soft-deleted row releases its
# name and an active row can retake it before the restore. (An earlier note
# here claimed the branches were unreachable — that predated sql/012.)
# sql/028 gave accounts the same rule; that regression lives in
# test_account_name_rules.py.


@pytest.mark.asyncio
async def test_restore_category_blocks_on_name_collision(client, test_data):
    """create → delete → create same name → restore original ⇒ 409."""
    original_id = str(uuid.uuid4())
    usurper_id = str(uuid.uuid4())
    name = f"restore-collide-{uuid.uuid4()}"
    try:
        r = await client.post(
            "/v1/categories",
            json={"id": original_id, "name": name, "color": "#abc123"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        r = await client.delete(
            f"/v1/categories/{original_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text

        # Retake the name (different case — the rule is case-insensitive).
        r = await client.post(
            "/v1/categories",
            json={"id": usurper_id, "name": name.upper(), "color": "#abc123"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text

        r = await client.post(
            f"/v1/categories/{original_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 409, r.text
        assert "already exists" in r.json()["error"]["message"]
    finally:
        await _cleanup_category(original_id, test_data.user_id)
        await _cleanup_category(usurper_id, test_data.user_id)


@pytest.mark.asyncio
async def test_restore_hashtag_blocks_on_name_collision(client, test_data):
    """Same shape as the category collision, on the hashtag table."""
    original_id = str(uuid.uuid4())
    usurper_id = str(uuid.uuid4())
    name = f"restore-collide-{uuid.uuid4()}"
    try:
        r = await client.post(
            "/v1/hashtags",
            json={"id": original_id, "name": name},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        r = await client.delete(
            f"/v1/hashtags/{original_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text

        r = await client.post(
            "/v1/hashtags",
            json={"id": usurper_id, "name": name.upper()},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text

        r = await client.post(
            f"/v1/hashtags/{original_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 409, r.text
        assert "already exists" in r.json()["error"]["message"]
    finally:
        await _cleanup_hashtag(original_id, test_data.user_id)
        await _cleanup_hashtag(usurper_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Hashtag restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_hashtag_round_trip_does_not_relink_junctions(
    client, test_data,
):
    """Hashtag restore brings the hashtag row back as an empty label —
    cascaded junction rows from the delete are deliberately NOT
    re-activated (silently re-tagging transactions the user no longer
    wants labeled would surprise everyone).
    """
    hashtag_id = str(uuid.uuid4())
    await client.post(
        "/v1/hashtags",
        json={"id": hashtag_id, "name": f"restore-tag-{uuid.uuid4()}"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )

    # Tag a transaction with this hashtag so the delete cascades junctions.
    txn_id = str(uuid.uuid4())
    create_txn_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"tagged-txn-{uuid.uuid4()}",
            "amount_cents": -100,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [hashtag_id],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn_r.status_code == 201, create_txn_r.text

    try:
        # Delete the hashtag — cascades junction rows.
        del_r = await client.delete(
            f"/v1/hashtags/{hashtag_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert del_r.status_code == 200, del_r.text

        # Restore the hashtag.
        restore_r = await client.post(
            f"/v1/hashtags/{hashtag_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        assert restore_r.json()["deleted_at"] is None

        # Junction rows must STAY soft-deleted — restore is intentionally not
        # cascading the relinking.
        async with db.pool.acquire() as conn:
            active_junctions = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transaction_hashtags
                WHERE hashtag_id = $1 AND deleted_at IS NULL
                """,
                hashtag_id,
            )
        assert active_junctions == 0, (
            f"Hashtag restore must not silently re-link junctions; "
            f"found {active_junctions} active rows"
        )

        assert await _activity_actions(hashtag_id, test_data.user_id) == [1, 3, 4]

    finally:
        # Hard-delete the test transaction first (to release its
        # reference to the hashtag), then the hashtag itself.
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
        await _cleanup_hashtag(hashtag_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Reconciliation restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_reconciliation_round_trip(client, test_data):
    """Create → delete → restore a reconciliation."""
    recon_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/reconciliations",
        json={
            "id": recon_id,
            "account_id": test_data.account_id,
            "name": f"restore-recon-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        del_r = await client.delete(
            f"/v1/reconciliations/{recon_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert del_r.status_code == 200, del_r.text

        restore_r = await client.post(
            f"/v1/reconciliations/{recon_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        body = restore_r.json()
        assert body["deleted_at"] is None
        # Native balances only — a reconciliation is scoped to one account and
        # therefore to one currency, so it has nothing to convert (WP2).
        assert body["beginning_balance_cents"] is not None
        assert body["ending_balance_cents"] is not None
        assert "beginning_balance_home_cents" not in body
        assert "ending_balance_home_cents" not in body

        assert await _activity_actions(recon_id, test_data.user_id) == [1, 3, 4]

    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Inbox — the exception: dismissing a draft is final
# ---------------------------------------------------------------------------


def test_inbox_restore_route_does_not_exist():
    """No `POST /inbox/{id}/restore` is registered — owner decision 2026-08-14.

    Asserted against the route table rather than a response code so it
    fails for the right reason: a 404 could equally mean "route exists,
    row missing", which is what the two tests this replaced would have
    started returning if the route had been silently unregistered.
    """
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/v1/inbox/{inbox_id}" in paths, "fixture assumption — inbox routes moved"
    assert "/v1/inbox/{inbox_id}/restore" not in paths, (
        "The inbox restore route is back. A draft is not a financial record: "
        "dismissing it is final (engine-spec §Restore semantics, the inbox "
        "exception)."
    )


@pytest.mark.asyncio
async def test_dismissed_inbox_item_stays_dismissed(client, test_data):
    """The delete is soft and the data survives — there is just no way back.

    Pins both halves of the decision: `?include_deleted=true` still reads
    the dismissed draft (nothing is erased), and the restore attempt 404s
    because no such route exists.
    """
    inbox_id = str(uuid.uuid4())
    await client.post(
        "/v1/inbox",
        json={"id": inbox_id, "title": f"dismissed-{uuid.uuid4()}"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )

    try:
        delete_r = await client.delete(
            f"/v1/inbox/{inbox_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert delete_r.status_code == 200, delete_r.text
        assert delete_r.json()["deleted_at"] is not None
        assert delete_r.json()["status"] == 1  # PENDING + deleted, not PROMOTED

        restore_r = await client.post(
            f"/v1/inbox/{inbox_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 404, restore_r.text

        # Soft, not hard: the row is still readable on request.
        listed = await client.get("/v1/inbox?include_deleted=true&limit=200")
        assert listed.status_code == 200, listed.text
        assert inbox_id in {item["id"] for item in listed.json()["items"]}

        # CREATED then DELETED — and no RESTORED, ever.
        assert await _activity_actions(inbox_id, test_data.user_id) == [1, 3]

    finally:
        await _cleanup_inbox(inbox_id, test_data.user_id)
