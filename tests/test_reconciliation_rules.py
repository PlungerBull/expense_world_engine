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


@pytest.mark.asyncio
async def test_complete_rejects_no_assigned_transactions(client, test_data):
    """Completing a reconciliation with zero assigned transactions is a
    422 — there is nothing to reconcile. Pins the guard message before
    the complete/revert twin collapse (engine-spec rule, previously
    untested)."""
    recon_id = str(uuid.uuid4())
    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": recon_id,
            "account_id": test_data.account_id,
            "name": f"no-assigned-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201, recon_create.text

    try:
        r = await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert (error.get("fields") or {}).get("transactions") == (
            "At least one transaction must be assigned."
        ), f"Guard message drifted: {error}"
    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)


async def _count_recon_activity(recon_id: str, user_id: str) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            recon_id, user_id,
        )


@pytest.mark.asyncio
async def test_complete_twice_is_noop_without_activity_log(client, test_data):
    """A second complete on an already-COMPLETED reconciliation returns
    200 with the current state and writes NO activity row — the no-op
    early return, pinned before the twin collapse."""
    txn_create = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"noop-complete-{uuid.uuid4()}",
            "amount_cents": -150,
            "date": "2026-04-12T10:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert txn_create.status_code == 201
    txn_id = txn_create.json()["id"]

    recon_id = str(uuid.uuid4())
    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": recon_id,
            "account_id": test_data.account_id,
            "name": f"noop-complete-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201
    try:
        await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        first = await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert first.status_code == 200, first.text

        count_after_first = await _count_recon_activity(recon_id, test_data.user_id)

        # Fresh idempotency key — this is a genuine second request, not a replay.
        second = await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == 2  # still COMPLETED

        count_after_second = await _count_recon_activity(recon_id, test_data.user_id)
        assert count_after_second == count_after_first, (
            "No-op complete must not write an activity row"
        )
    finally:
        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)


@pytest.mark.asyncio
async def test_revert_draft_is_noop_without_activity_log(client, test_data):
    """Reverting a never-completed DRAFT returns 200 with the current
    state and writes NO activity row — the mirror-image no-op."""
    recon_id = str(uuid.uuid4())
    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": recon_id,
            "account_id": test_data.account_id,
            "name": f"noop-revert-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201
    try:
        count_before = await _count_recon_activity(recon_id, test_data.user_id)

        r = await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == 1  # still DRAFT

        count_after = await _count_recon_activity(recon_id, test_data.user_id)
        assert count_after == count_before, (
            "No-op revert must not write an activity row"
        )
    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)


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


# ---------------------------------------------------------------------------
# Bug 5.5 — completed reconciliations freeze membership and delete, one PUT
# is one version bump, and delete/restore races 409 instead of 500.
# ---------------------------------------------------------------------------


async def _txn_and_recon(client, test_data, *, complete: bool):
    """Create a transaction, a reconciliation on the same account, assign,
    and optionally complete. Returns (txn_id, recon_id)."""
    txn_create = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"bug55-{uuid.uuid4()}",
            "amount_cents": -500,
            "date": "2026-04-12T10:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert txn_create.status_code == 201, txn_create.text
    txn_id = txn_create.json()["id"]

    recon_create = await client.post(
        "/v1/reconciliations",
        json={
            "id": str(uuid.uuid4()),
            "account_id": test_data.account_id,
            "name": f"bug55-{uuid.uuid4()}",
            "beginning_balance_cents": 0,
            "ending_balance_cents": 0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert recon_create.status_code == 201, recon_create.text
    recon_id = recon_create.json()["id"]

    assign = await client.put(
        f"/v1/transactions/{txn_id}",
        json={"reconciliation_id": recon_id},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert assign.status_code == 200, assign.text

    if complete:
        complete_r = await client.post(
            f"/v1/reconciliations/{recon_id}/complete",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert complete_r.status_code == 200, complete_r.text

    return txn_id, recon_id


@pytest.mark.asyncio
async def test_completed_reconciliation_locks_assignment(client, test_data):
    """Unassigning (null) or moving a transaction out of a completed
    reconciliation is locked exactly like the amount/account/title/date
    fields — 422 naming reconciliation_id. Reverting unlocks it.
    """
    txn_id, recon_id = await _txn_and_recon(client, test_data, complete=True)
    other_recon_id = None
    try:
        # Unassign (explicit null) — locked.
        unassign = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": None},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert unassign.status_code == 422, unassign.text
        assert "reconciliation_id" in (unassign.json()["error"].get("fields") or {})

        # Move to a different (draft) reconciliation — equally locked.
        other = await client.post(
            "/v1/reconciliations",
            json={
                "id": str(uuid.uuid4()),
                "account_id": test_data.account_id,
                "name": f"bug55-other-{uuid.uuid4()}",
                "beginning_balance_cents": 0,
                "ending_balance_cents": 0,
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert other.status_code == 201, other.text
        other_recon_id = other.json()["id"]
        move = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": other_recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert move.status_code == 422, move.text
        assert "reconciliation_id" in (move.json()["error"].get("fields") or {})

        # The lock reads the live status: revert, then unassign succeeds.
        revert = await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert revert.status_code == 200, revert.text
        unassign_ok = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": None},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert unassign_ok.status_code == 200, unassign_ok.text
        assert unassign_ok.json()["reconciliation_id"] is None
    finally:
        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        if other_recon_id is not None:
            await _cleanup_reconciliation(other_recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)


@pytest.mark.asyncio
async def test_completed_reconciliation_blocks_delete(client, test_data):
    """Deleting a transaction assigned to a completed reconciliation is a
    409 that leaves the row untouched; after a revert the delete succeeds.
    (Replaced the old warn-but-allow behaviour — owner decision 2026-08-11.)
    """
    txn_id, recon_id = await _txn_and_recon(client, test_data, complete=True)
    try:
        blocked = await client.delete(
            f"/v1/transactions/{txn_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "CONFLICT"

        # The 409 must not have mutated anything.
        still_there = await client.get(f"/v1/transactions/{txn_id}")
        assert still_there.status_code == 200
        assert still_there.json()["deleted_at"] is None

        revert = await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert revert.status_code == 200, revert.text
        allowed = await client.delete(
            f"/v1/transactions/{txn_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert allowed.status_code == 200, allowed.text
        # No warnings envelope on delete — restore is the sole member now.
        assert "warnings" not in allowed.json()
    finally:
        await client.post(
            f"/v1/reconciliations/{recon_id}/revert",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)


@pytest.mark.asyncio
async def test_put_with_reconciliation_change_bumps_version_once(client, test_data):
    """A PUT combining reconciliation_id with another field moves version by
    exactly one — the double bump broke read-modify-write conflict detection.
    """
    txn_id, recon_id = await _txn_and_recon(client, test_data, complete=False)
    try:
        before = await client.get(f"/v1/transactions/{txn_id}")
        assert before.status_code == 200
        v0 = before.json()["version"]

        combined = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": None, "description": "one bump"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert combined.status_code == 200, combined.text
        assert combined.json()["reconciliation_id"] is None
        assert combined.json()["version"] == v0 + 1, (
            f"Expected a single version bump ({v0} -> {v0 + 1}), "
            f"got {combined.json()['version']}"
        )

        # Reconciliation-only change is also exactly one bump.
        reassign = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": recon_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert reassign.status_code == 200, reassign.text
        assert reassign.json()["version"] == v0 + 2
    finally:
        await _cleanup_reconciliation(recon_id, test_data.user_id)
        await _cleanup_transactions([txn_id], test_data.user_id)


@pytest.mark.asyncio
async def test_restore_race_is_409_not_500(client, test_data):
    """If the row a restore was asked to revive got restored (or was never
    deleted) by the time the UPDATE runs, the shared audit helper must
    refuse with a 409 CONFLICT — not serialize None into a TypeError/500.
    Exercised at the helper layer because over the wire the pre-fetch 404s
    first; the gap is precisely the fetch→mutate race window.
    """
    from app.errors import AppError
    from app.helpers.query_builder import fetch_owned_row_or_404, restore_with_audit
    from app.schemas.categories import category_from_row

    cat_create = await client.post(
        "/v1/categories",
        json={
            "id": str(uuid.uuid4()),
            "name": f"race-{uuid.uuid4().hex[:12]}",
            "color": "#00AA00",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert cat_create.status_code == 201, cat_create.text
    cat_id = cat_create.json()["id"]

    try:
        deleted = await client.delete(
            f"/v1/categories/{cat_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert deleted.status_code == 200, deleted.text

        async with db.pool.acquire() as conn:
            # The stale pre-fetch a real request would have made...
            stale_row = await fetch_owned_row_or_404(
                conn, "expense_categories", cat_id, test_data.user_id,
                "category", deleted=True,
            )
            # ...then a concurrent request wins the restore race...
            await conn.execute(
                "UPDATE expense_categories SET deleted_at = NULL WHERE id = $1",
                cat_id,
            )
            # ...and the helper must 409, not crash.
            with pytest.raises(AppError) as exc_info:
                await restore_with_audit(
                    conn, test_data.user_id, "expense_categories", "category",
                    stale_row, category_from_row,
                )
            assert exc_info.value.status_code == 409
            assert exc_info.value.code == "CONFLICT"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                cat_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_categories WHERE id = $1 AND user_id = $2",
                cat_id, test_data.user_id,
            )
