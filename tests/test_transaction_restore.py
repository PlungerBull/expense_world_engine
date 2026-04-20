"""Regression tests for ``POST /transactions/{id}/restore``.

Restore is the inverse of ``delete_transaction``. The flow re-applies
the balance impact, re-activates cascaded hashtag junction rows,
cascades to the transfer sibling for transfer pairs, and conditionally
clears the reconciliation link based on the recon's current state.

The tests below cover the five paths most likely to regress:

  * Simple round-trip: delete then restore leaves balance and version
    consistent.
  * Transfer pairs restore atomically — both legs come back, both
    balances move together, both activity log entries are written.
  * Cascaded hashtag junction rows are re-activated by ``deleted_at``
    timestamp match (the only way to distinguish "cascaded by THIS
    delete" from "soft-deleted earlier by some other operation").
  * Restoring a transaction whose previous reconciliation is now
    COMPLETED unlinks it (the locked-fields invariant would otherwise
    leave the row immutable) and surfaces the change as a warning.
  * Validation guards are checked BEFORE any mutation — a 422 on a
    soft-deleted transaction whose account got archived must leave the
    soft-deleted row untouched.

Run: .venv/bin/pytest tests/test_transaction_restore.py -v
"""
import uuid

import pytest

from app import db


# ---------------------------------------------------------------------------
# Helpers (mirror test_concurrency_hazards.py patterns)
# ---------------------------------------------------------------------------


async def _new_expense(client, account_id: str, category_id: str, amount: int) -> dict:
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"restore-{uuid.uuid4()}",
            "amount_cents": -amount,
            "date": "2026-04-12T12:00:00Z",
            "account_id": account_id,
            "category_id": category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _get_balance(account_id: str) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT current_balance_cents FROM expense_bank_accounts WHERE id = $1",
            account_id,
        )


async def _get_row(transaction_id: str) -> dict:
    async with db.pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM expense_transactions WHERE id = $1",
            transaction_id,
        )


async def _hard_delete_txns(txn_ids: list[str], user_id: str) -> None:
    """Hard-delete transactions + junction + activity rows. Cleanup only."""
    if not txn_ids:
        return
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE transaction_id = ANY($1::uuid[]) AND user_id = $2",
            txn_ids, user_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[]) AND user_id = $2",
            txn_ids, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[]) AND user_id = $2",
            txn_ids, user_id,
        )


async def _restore_balance(account_id: str, user_id: str, target: int) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET current_balance_cents = $1 WHERE id = $2 AND user_id = $3",
            target, account_id, user_id,
        )


# ---------------------------------------------------------------------------
# 1. Simple round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_simple_expense_round_trips_balance(client, test_data):
    """Create → delete → restore.

    After restore, the transaction is active again and the account
    balance equals the pre-delete balance (i.e. the delete's reversal
    was inverted by restore's re-apply). The activity log shows
    CREATED → DELETED → RESTORED.
    """
    created = await _new_expense(
        client, test_data.account_id, test_data.category_id, amount=1000,
    )
    txn_id = created["id"]
    balance_after_create = await _get_balance(test_data.account_id)

    try:
        # Delete: balance should rise by 1000 (the expense's reversal).
        delete_r = await client.delete(
            f"/v1/transactions/{txn_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert delete_r.status_code == 200, delete_r.text
        assert delete_r.json()["warnings"] == []
        balance_after_delete = await _get_balance(test_data.account_id)
        assert balance_after_delete == balance_after_create + 1000

        # Restore: balance should fall by 1000 again.
        restore_r = await client.post(
            f"/v1/transactions/{txn_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        body = restore_r.json()
        assert body["warnings"] == []
        assert body["deleted_at"] is None
        assert body["amount_cents"] == 1000  # storage is positive

        balance_after_restore = await _get_balance(test_data.account_id)
        assert balance_after_restore == balance_after_create, (
            f"Balance did not round-trip: create={balance_after_create} "
            f"delete={balance_after_delete} restore={balance_after_restore}"
        )

        # Activity log trail: CREATED (1) + DELETED (3) + RESTORED (4).
        async with db.pool.acquire() as conn:
            actions = await conn.fetch(
                """
                SELECT action FROM activity_log
                WHERE resource_id = $1 AND user_id = $2
                ORDER BY created_at ASC
                """,
                txn_id, test_data.user_id,
            )
        action_codes = [r["action"] for r in actions]
        assert action_codes == [1, 3, 4], (
            f"Expected CREATED → DELETED → RESTORED, got {action_codes}"
        )

    finally:
        await _hard_delete_txns([txn_id], test_data.user_id)
        await _restore_balance(test_data.account_id, test_data.user_id, balance_after_create - 1000)


# ---------------------------------------------------------------------------
# 2. Transfer-pair atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_transfer_pair_restores_both_legs_atomically(client, test_data):
    """Create transfer → delete (cascades) → restore primary.

    Both legs come back (deleted_at NULL on both), both balances move
    together, both legs have RESTORED activity entries, reciprocal
    transfer_transaction_id links remain intact.
    """
    second_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, 'Restore-Transfer-Target', 'PEN', false, '#00FF00',
                    50000, false, 2, now(), now())
            """,
            second_account_id, test_data.user_id,
        )

    primary_id = sibling_id = None
    before_primary_balance = await _get_balance(test_data.account_id)
    before_secondary_balance = await _get_balance(second_account_id)

    try:
        primary_uuid = str(uuid.uuid4())
        sibling_uuid = str(uuid.uuid4())
        create_r = await client.post(
            "/v1/transactions",
            json={
                "id": primary_uuid,
                "title": f"restore-transfer-{uuid.uuid4()}",
                "amount_cents": -2500,
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
                "transfer": {
                    "id": sibling_uuid,
                    "account_id": second_account_id,
                    "amount_cents": 2500,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert create_r.status_code == 201, create_r.text
        primary_id = create_r.json()["id"]
        sibling_id = create_r.json()["transfer_transaction_id"]

        # Delete cascades to sibling.
        delete_r = await client.delete(
            f"/v1/transactions/{primary_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert delete_r.status_code == 200, delete_r.text

        # Restore primary — should cascade-restore the sibling.
        restore_r = await client.post(
            f"/v1/transactions/{primary_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text

        # Both legs active again, both balances re-applied.
        primary_row = await _get_row(primary_id)
        sibling_row = await _get_row(sibling_id)
        assert primary_row["deleted_at"] is None
        assert sibling_row["deleted_at"] is None
        assert str(primary_row["transfer_transaction_id"]) == sibling_id
        assert str(sibling_row["transfer_transaction_id"]) == primary_id

        after_primary_balance = await _get_balance(test_data.account_id)
        after_secondary_balance = await _get_balance(second_account_id)
        assert after_primary_balance == before_primary_balance - 2500
        assert after_secondary_balance == before_secondary_balance + 2500

        # Activity trail per leg: CREATED, DELETED, RESTORED.
        async with db.pool.acquire() as conn:
            for tid in (primary_id, sibling_id):
                actions = await conn.fetch(
                    """
                    SELECT action FROM activity_log
                    WHERE resource_id = $1 AND user_id = $2
                    ORDER BY created_at ASC
                    """,
                    tid, test_data.user_id,
                )
                codes = [r["action"] for r in actions]
                assert codes == [1, 3, 4], (
                    f"Leg {tid}: expected CREATED → DELETED → RESTORED, got {codes}"
                )

    finally:
        ids = [tid for tid in (primary_id, sibling_id) if tid]
        async with db.pool.acquire() as conn:
            if ids:
                await conn.execute(
                    "DELETE FROM expense_transaction_hashtags WHERE transaction_id = ANY($1::uuid[]) AND user_id = $2",
                    ids, test_data.user_id,
                )
                await conn.execute(
                    "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[]) AND user_id = $2",
                    ids, test_data.user_id,
                )
                await conn.execute(
                    "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[]) AND user_id = $2",
                    ids, test_data.user_id,
                )
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = $1 WHERE id = $2",
                before_primary_balance, test_data.account_id,
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                second_account_id, test_data.user_id,
            )


# ---------------------------------------------------------------------------
# 3. Cascaded hashtag junction restoration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_re_attaches_cascaded_hashtags(client, test_data):
    """Junction rows soft-deleted by the delete cascade are re-activated.

    The match is precise: only junction rows whose ``deleted_at`` equals
    the parent's are restored, so pre-existing soft-deleted junctions
    from earlier ``_sync_hashtags`` runs stay deleted.
    """
    txn_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"restore-hashtags-{uuid.uuid4()}",
            "amount_cents": -100,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [test_data.hashtag_id, test_data.hashtag2_id],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text
    balance_after_create = await _get_balance(test_data.account_id)

    try:
        # Verify both junctions active before delete.
        async with db.pool.acquire() as conn:
            active_before = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transaction_hashtags
                WHERE transaction_id = $1 AND deleted_at IS NULL
                """,
                txn_id,
            )
            assert active_before == 2

        await client.delete(
            f"/v1/transactions/{txn_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )

        # All junctions soft-deleted by cascade.
        async with db.pool.acquire() as conn:
            active_after_delete = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transaction_hashtags
                WHERE transaction_id = $1 AND deleted_at IS NULL
                """,
                txn_id,
            )
            assert active_after_delete == 0

        await client.post(
            f"/v1/transactions/{txn_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )

        # Both junctions back, matching the count before delete.
        async with db.pool.acquire() as conn:
            active_after_restore = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transaction_hashtags
                WHERE transaction_id = $1 AND deleted_at IS NULL
                """,
                txn_id,
            )
            assert active_after_restore == 2, (
                f"Expected 2 active junctions after restore, got {active_after_restore}"
            )

    finally:
        await _hard_delete_txns([txn_id], test_data.user_id)
        await _restore_balance(test_data.account_id, test_data.user_id, balance_after_create - 100)


# ---------------------------------------------------------------------------
# 4. Reconciliation unlink on completed-recon restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_unlinks_completed_reconciliation(client, test_data):
    """Restoring a transaction whose recon is now COMPLETED clears the
    link and emits a warning.

    Re-linking would leave the row with frozen fields (the locked-fields
    invariant on completed reconciliations), which the user wouldn't
    expect after an undo.
    """
    txn_id = str(uuid.uuid4())
    recon_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"restore-recon-{uuid.uuid4()}",
            "amount_cents": -300,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text
    balance_after_create = await _get_balance(test_data.account_id)

    recon_create = await client.post(
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
    assert recon_create.status_code == 201, recon_create.text

    try:
        # Assign the txn to the recon, complete the recon, delete the txn.
        await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await client.delete(
            f"/v1/transactions/{txn_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )

        # Restore — should unlink and warn.
        restore_r = await client.post(
            f"/v1/transactions/{txn_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 200, restore_r.text
        body = restore_r.json()

        assert body["reconciliation_id"] is None, (
            f"Restored row should have null reconciliation_id, got {body['reconciliation_id']}"
        )
        warnings = body.get("warnings", [])
        assert any("reconciliation" in w.lower() for w in warnings), (
            f"Expected a reconciliation-related warning, got {warnings}"
        )

    finally:
        # Revert the recon so cleanup can proceed (completed recons can't be deleted).
        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE expense_transactions SET reconciliation_id = NULL WHERE reconciliation_id = $1",
                recon_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                recon_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_reconciliations WHERE id = $1 AND user_id = $2",
                recon_id, test_data.user_id,
            )
        await _hard_delete_txns([txn_id], test_data.user_id)
        await _restore_balance(test_data.account_id, test_data.user_id, balance_after_create - 300)


# ---------------------------------------------------------------------------
# 5. Validation guard — archived account blocks restore, leaves state untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_blocked_when_account_archived(client, test_data):
    """If the txn's account got archived after the delete, restore must
    return 422 with field-level error and leave the soft-deleted row
    untouched (no partial mutation).
    """
    fresh_account_id = str(uuid.uuid4())
    create_acc_r = await client.post(
        "/v1/accounts",
        json={
            "id": fresh_account_id,
            "name": f"restore-blocked-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_acc_r.status_code == 201, create_acc_r.text

    txn_id = str(uuid.uuid4())
    create_txn_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"restore-blocked-{uuid.uuid4()}",
            "amount_cents": -750,
            "date": "2026-04-12T12:00:00Z",
            "account_id": fresh_account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn_r.status_code == 201, create_txn_r.text

    try:
        # Delete the txn, then archive the account.
        await client.delete(
            f"/v1/transactions/{txn_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        archive_r = await client.post(
            f"/v1/accounts/{fresh_account_id}/archive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert archive_r.status_code == 200, archive_r.text

        # Snapshot the soft-deleted row's state — must be unchanged after the failed restore.
        snapshot_before = await _get_row(txn_id)
        assert snapshot_before["deleted_at"] is not None
        version_before = snapshot_before["version"]

        # Attempt restore — must fail with 422 + field-level account_id error.
        restore_r = await client.post(
            f"/v1/transactions/{txn_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 422, restore_r.text
        error = restore_r.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert "account_id" in (error.get("fields") or {}), (
            f"Expected account_id in fields, got {error}"
        )

        # Soft-deleted row must be untouched — same deleted_at, same version.
        snapshot_after = await _get_row(txn_id)
        assert snapshot_after["deleted_at"] == snapshot_before["deleted_at"]
        assert snapshot_after["version"] == version_before

    finally:
        await _hard_delete_txns([txn_id], test_data.user_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                fresh_account_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                fresh_account_id, test_data.user_id,
            )
