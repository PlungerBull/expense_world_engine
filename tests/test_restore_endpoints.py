"""Regression tests for the per-resource restore endpoints.

The transaction restore path has its own dedicated test file
(`test_transaction_restore.py`) because its inverse logic is intricate
(balance re-apply, junction precision, transfer sibling cascade,
reconciliation handling). The five endpoints covered here are the
"simpler" restores — accounts, categories, hashtags, reconciliations,
and pending inbox items — but each carries its own resource-specific
guard rail that this file pins down:

  * Accounts: round-trip (clear deleted_at, RESTORED activity entry).
  * Categories: same, plus a name-collision check that returns 409 when
    an active category has taken over the deleted one's display name.
  * Hashtags: same name-collision check, AND the deliberate decision
    NOT to re-link cascaded junction rows (restoring would silently
    re-tag transactions the user may no longer want labeled).
  * Reconciliations: round-trip (transactions unassigned during the
    delete are NOT re-linked on restore — same reasoning as hashtags).
  * Inbox: PENDING items restore cleanly; PROMOTED items return 409
    pointing the client at the ledger transaction (the underlying
    promote created a ledger row that's still alive, so restoring the
    inbox row would put the user one promote-click away from a
    duplicate ledger entry).

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


# NOTE on the name-collision branches in restore_category and restore_hashtag:
# Both helpers contain a defensive check that returns 409 if an active row
# with the same display name exists when restoring. That branch is
# CURRENTLY UNREACHABLE from the public API because expense_categories and
# expense_hashtags both carry full UNIQUE (user_id, name) constraints
# (no partial WHERE deleted_at IS NULL). A soft-deleted row keeps its name
# locked, so a clash can never be created. The defensive check exists as
# belt-and-braces in case the constraint is ever relaxed to a partial
# unique index (e.g. to allow soft-deleted name reuse). When that happens,
# add a regression test here. Tested-via-DB-constraint for now.


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
            # Restore the test_data account balance (we created a -100 expense).
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 100 WHERE id = $1",
                test_data.account_id,
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
        # Home-cents fields populated for active rows.
        assert "beginning_balance_home_cents" in body
        assert "ending_balance_home_cents" in body

        assert await _activity_actions(recon_id, test_data.user_id) == [1, 3, 4]

    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Inbox restore — pending OK, promoted blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_pending_inbox_item_round_trip(client, test_data):
    """A dismissed (PENDING + deleted_at) inbox item restores cleanly."""
    inbox_id = str(uuid.uuid4())
    await client.post(
        "/v1/inbox",
        json={"id": inbox_id, "title": f"restore-inbox-{uuid.uuid4()}"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )

    try:
        await client.delete(
            f"/v1/inbox/{inbox_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        restore_r = await client.post(
            f"/v1/inbox/{inbox_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        body = restore_r.json()
        assert body["deleted_at"] is None
        assert body["status"] == 1  # PENDING

        assert await _activity_actions(inbox_id, test_data.user_id) == [1, 3, 4]

    finally:
        await _cleanup_inbox(inbox_id, test_data.user_id)


@pytest.mark.asyncio
async def test_restore_promoted_inbox_item_returns_409(client, test_data):
    """A promoted inbox item (status=2) is soft-deleted as part of the
    promote flow but is NOT restorable here — the ledger transaction it
    created still exists, so restoring would put the user one
    promote-click from a duplicate ledger row. The 409 message points
    the client at the ledger.
    """
    inbox_id = str(uuid.uuid4())
    txn_id = str(uuid.uuid4())

    # Create a fully-formed inbox item ready to promote.
    create_r = await client.post(
        "/v1/inbox",
        json={
            "id": inbox_id,
            "title": f"promotable-{uuid.uuid4()}",
            "amount_cents": -250,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    promote_r = await client.post(
        f"/v1/inbox/{inbox_id}/promote",
        json={"id": txn_id},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert promote_r.status_code == 200, promote_r.text

    try:
        # Inbox row is now status=2 + deleted_at IS NOT NULL.
        async with db.pool.acquire() as conn:
            inbox_row = await conn.fetchrow(
                "SELECT status, deleted_at FROM expense_transaction_inbox WHERE id = $1",
                inbox_id,
            )
        assert inbox_row["status"] == 2
        assert inbox_row["deleted_at"] is not None

        restore_r = await client.post(
            f"/v1/inbox/{inbox_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 409, restore_r.text
        error = restore_r.json()["error"]
        assert error["code"] == "CONFLICT"
        assert "ledger" in error["message"].lower(), (
            f"409 message should redirect to the ledger; got {error['message']!r}"
        )

        # State unchanged — inbox row still soft-deleted with status=2.
        async with db.pool.acquire() as conn:
            inbox_row_after = await conn.fetchrow(
                "SELECT status, deleted_at FROM expense_transaction_inbox WHERE id = $1",
                inbox_id,
            )
        assert inbox_row_after["status"] == 2
        assert inbox_row_after["deleted_at"] is not None

    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[]) AND user_id = $2",
                [inbox_id, txn_id], test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )
            # Restore test_data account balance (the promote applied -250).
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 250 WHERE id = $1",
                test_data.account_id,
            )
