"""Regression tests for two reconciliation rules flagged by the audit.

  * **Field-locking on completed reconciliations** — once a
    reconciliation's status is COMPLETED, certain fields on any
    transaction assigned to it become immutable (amount_cents,
    account_id, title, date). This prevents silently rewriting the
    history that produced a "matching" reconciled balance.

  * **Detail endpoint pagination** — ``GET /reconciliations/{id}`` pages
    the embedded transactions list via ``limit`` / ``offset`` params
    and echoes ``transactions_total`` plus a ``transactions_truncated``
    flag so clients know when to page.

Run: .venv/bin/pytest tests/test_reconciliation_rules.py -v
"""
import uuid

import pytest

from app import db


async def _cleanup_reconciliation(recon_id: str, user_id: str) -> None:
    """Unassign all related transactions and hard-delete the reconciliation."""
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


async def _cleanup_transactions(txn_ids: list[str], user_id: str) -> None:
    if not txn_ids:
        return
    async with db.pool.acquire() as conn:
        for tid in txn_ids:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                tid, user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
                tid, user_id,
            )
        await conn.execute(
            "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[]) AND user_id = $2",
            txn_ids, user_id,
        )


@pytest.mark.asyncio
async def test_completed_reconciliation_locks_transaction_fields(client, test_data):
    """Once a reconciliation is COMPLETED, amount_cents on an assigned
    transaction cannot be modified — the PUT must fail with 422 and
    the field-level error must identify the offending key.
    """
    # Create a dedicated transaction for this test to avoid interacting
    # with the seeded test_data transaction, which other tests depend on.
    txn_create = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"recon-lock-{uuid.uuid4()}",
            "amount_cents": -500,
            "date": "2026-04-12T10:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert txn_create.status_code == 201
    txn_id = txn_create.json()["id"]

    # Create a reconciliation on the same account.
    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": str(uuid.uuid4()),
            "account_id": test_data.account_id,
            "name": f"recon-lock-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201
    recon_id = recon_create.json()["id"]

    try:
        # Assign the transaction to the reconciliation.
        assign = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert assign.status_code == 200, assign.text

        # Mark the reconciliation as completed — this is what triggers
        # field locking on all assigned transactions.
        complete = await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert complete.status_code == 200, complete.text

        # Now attempt to change amount_cents — must be rejected.
        bad_update = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"amount_cents": -999},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert bad_update.status_code == 422, (
            f"Expected 422 on locked field update, got {bad_update.status_code}: {bad_update.text}"
        )
        error_body = bad_update.json()["error"]
        assert "amount_cents" in (error_body.get("fields") or {}), (
            f"Error should name amount_cents as locked; got {error_body}"
        )

        # Non-locked fields should still be updatable. ``description``
        # is explicitly NOT in the locked set.
        ok_update = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"description": "safe to edit"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert ok_update.status_code == 200, (
            f"Non-locked field should be updatable on a completed-reconciliation txn, "
            f"got {ok_update.status_code}: {ok_update.text}"
        )

    finally:
        # Revert the reconciliation so the cleanup can unassign txns.
        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)
        # Restore account balance.
        async with db.pool.acquire() as conn:
            # The test created one expense of -500 which was reversed
            # during deletion by the regular delete path... except we
            # used raw DELETE for cleanup, not the soft-delete endpoint.
            # So we must manually credit the 500 back.
            await conn.execute(
                """
                UPDATE expense_bank_accounts
                SET current_balance_cents = current_balance_cents + 500
                WHERE id = $1 AND user_id = $2
                """,
                test_data.account_id, test_data.user_id,
            )


@pytest.mark.asyncio
async def test_completed_reconciliation_rejects_balance_edits(client, test_data):
    """PUT /reconciliations/{id} on a COMPLETED row must reject edits to
    the locked set (beginning_balance_cents, ending_balance_cents,
    date_start, date_end) with a 422 and name them in error.fields. Name
    stays editable."""
    # Create a txn so we can complete the recon.
    txn_create = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"lock-recon-{uuid.uuid4()}",
            "amount_cents": -250,
            "date": "2026-04-12T10:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert txn_create.status_code == 201
    txn_id = txn_create.json()["id"]

    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": str(uuid.uuid4()),
            "account_id": test_data.account_id,
            "name": f"lock-recon-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201
    recon_id = recon_create.json()["id"]

    try:
        await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        complete = await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert complete.status_code == 200, complete.text

        # Attempt to edit locked balance field — must fail.
        bad = await client.put(
            f"/v1/reconciliations/{recon_id}",
            json={"ending_balance_cents": 12345},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert bad.status_code == 422, bad.text
        assert "ending_balance_cents" in (bad.json()["error"].get("fields") or {})

        # Name stays editable on COMPLETED.
        ok = await client.put(
            f"/v1/reconciliations/{recon_id}",
            json={"name": "renamed-after-complete"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["name"] == "renamed-after-complete"
    finally:
        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 250 WHERE id = $1 AND user_id = $2",
                test_data.account_id, test_data.user_id,
            )


@pytest.mark.asyncio
async def test_recon_complete_and_revert_bump_assigned_txn_version(client, test_data):
    """complete_reconciliation and revert_reconciliation must increment
    the version of every assigned transaction atomically with the status
    flip so delta-sync clients see the lock state change."""
    txn_create = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"version-bump-{uuid.uuid4()}",
            "amount_cents": -100,
            "date": "2026-04-12T10:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert txn_create.status_code == 201
    txn_id = txn_create.json()["id"]

    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": str(uuid.uuid4()),
            "account_id": test_data.account_id,
            "name": f"version-bump-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201
    recon_id = recon_create.json()["id"]

    try:
        await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        # Capture the version AFTER the assign-put.
        pre_version = (await client.get(f"/v1/transactions/{txn_id}")).json()["version"]

        await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        after_complete = (await client.get(f"/v1/transactions/{txn_id}")).json()["version"]
        assert after_complete > pre_version, (
            f"Transaction version must bump on complete: was {pre_version}, got {after_complete}"
        )

        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        after_revert = (await client.get(f"/v1/transactions/{txn_id}")).json()["version"]
        assert after_revert > after_complete, (
            f"Transaction version must bump on revert: was {after_complete}, got {after_revert}"
        )
    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 100 WHERE id = $1 AND user_id = $2",
                test_data.account_id, test_data.user_id,
            )


@pytest.mark.asyncio
async def test_reconciliation_transactions_paginate_and_flag_truncation(
    client, test_data,
):
    """GET /reconciliations/{id} returns a paged window of embedded
    transactions controlled by ``limit`` / ``offset`` params. The
    response echoes ``transactions_total`` and sets
    ``transactions_truncated = True`` whenever there are more rows
    beyond the current page.
    """
    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": str(uuid.uuid4()),
            "account_id": test_data.account_id,
            "name": f"page-test-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201
    recon_id = recon_create.json()["id"]

    created_txn_ids: list[str] = []
    try:
        # Create 4 transactions, assigning each to the reconciliation.
        for i in range(4):
            create = await client.post(
                "/v1/transactions",
                json={
                    "id": str(uuid.uuid4()),
                    "title": f"page-txn-{i}-{uuid.uuid4()}",
                    "amount_cents": -100,
                    "date": "2026-04-12T10:00:00Z",
                    "account_id": test_data.account_id,
                    "category_id": test_data.category_id,
                },
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            )
            assert create.status_code == 201
            txn_id = create.json()["id"]
            created_txn_ids.append(txn_id)

            assign = await client.put(
                f"/v1/transactions/{txn_id}",
                json={"reconciliation_id": recon_id},
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            )
            assert assign.status_code == 200

        # First page of 3 — truncated=True, total=4.
        r = await client.get(f"/v1/reconciliations/{recon_id}?limit=3&offset=0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transactions_total"] == 4
        assert body["transactions_limit"] == 3
        assert body["transactions_offset"] == 0
        assert body["transactions_truncated"] is True
        assert len(body["transactions"]) == 3

        # Second page — 1 row, truncated=False.
        r2 = await client.get(f"/v1/reconciliations/{recon_id}?limit=3&offset=3")
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["transactions_total"] == 4
        assert body2["transactions_offset"] == 3
        assert body2["transactions_truncated"] is False
        assert len(body2["transactions"]) == 1

        # Larger window than total — returns all, truncated=False.
        r3 = await client.get(f"/v1/reconciliations/{recon_id}?limit=10")
        assert r3.status_code == 200
        body3 = r3.json()
        assert body3["transactions_truncated"] is False
        assert len(body3["transactions"]) == 4

    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions(created_txn_ids, test_data.user_id)
        # Restore balance: 4 expenses of -100 each = -400 total.
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE expense_bank_accounts
                SET current_balance_cents = current_balance_cents + 400
                WHERE id = $1 AND user_id = $2
                """,
                test_data.account_id, test_data.user_id,
            )
