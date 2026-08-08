"""WP6 — reconciliation chaining is gone; editing one row never touches another.

sql/025 dropped ``beginning_balance_source`` and ``sort_order`` and deleted the
chaining cascade (``_cascade_chained_recalc``), which rewrote downstream rows'
``beginning_balance_cents`` with NO status predicate — editing an upstream DRAFT
silently mutated a COMPLETED reconciliation's locked balance. The first test in
this file is the regression the whole package exists to prevent.

What replaced the machinery:

  * Beginning balance is required on POST and always user-entered. There is no
    derived mode and no prefill (owner decision 2026-08-06, superseding
    open-bugs decision D3).
  * Account-scoped lists order by ``date_start ASC NULLS LAST, created_at ASC``
    — a reconciliation is a statement period, so its date is its position.
  * ``difference_cents`` on every response: (ending − beginning) − signed sum
    of the assigned non-deleted transactions. Computed at read time from the
    ledger, never stored, exactly like account balances (WP3).

Seeding follows test_wp3's rules: accounts and transactions are created inline
and torn down in the fixture; no exchange rates are seeded and no home value is
asserted (reconciliations are native-only per WP2). Dates are past and outside
other files' rate windows.
"""
import json
import uuid

import asyncpg
import pytest

from app import db


SEED_DATE = "2024-03-15T12:00:00Z"


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


class Fixtures:
    def __init__(self):
        self.account_id = str(uuid.uuid4())
        self.second_account_id = str(uuid.uuid4())
        self.category_id = str(uuid.uuid4())
        self.recon_ids: list[str] = []
        self.txn_ids: list[str] = []


@pytest.fixture
async def fx(test_data, db_pool):
    """Two PEN accounts and a private category, all torn down afterwards."""
    data = Fixtures()

    async with db.pool.acquire() as conn:
        for account_id, name in (
            (data.account_id, "WP6-Primary"),
            (data.second_account_id, "WP6-Secondary"),
        ):
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at)
                   VALUES ($1, $2, $3, 'PEN', false, '#116644',
                           false, 97, now(), now())""",
                account_id, test_data.user_id, f"{name}-{account_id[:8]}",
            )
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, is_system, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, $3, '#116644', false, 97, now(), now())""",
            data.category_id, test_data.user_id, f"WP6-Cat-{data.category_id[:8]}",
        )

    yield data

    async with db.pool.acquire() as conn:
        account_ids = [data.account_id, data.second_account_id]
        await conn.execute(
            """DELETE FROM activity_log
               WHERE user_id = $1 AND resource_id = ANY($2::uuid[])""",
            test_data.user_id,
            account_ids + data.recon_ids + data.txn_ids,
        )
        await conn.execute(
            """DELETE FROM expense_transactions
               WHERE user_id = $1 AND account_id = ANY($2::uuid[])""",
            test_data.user_id, account_ids,
        )
        await conn.execute(
            """DELETE FROM expense_reconciliations
               WHERE user_id = $1 AND account_id = ANY($2::uuid[])""",
            test_data.user_id, account_ids,
        )
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = $1 AND user_id = $2",
            data.category_id, test_data.user_id,
        )
        await conn.execute(
            """DELETE FROM expense_bank_accounts
               WHERE id = ANY($1::uuid[]) AND user_id = $2""",
            account_ids, test_data.user_id,
        )


async def _create_recon(
    client, fx, *, name: str, beginning: int = 0, ending: int = 0,
    date_start=None, account_id=None,
) -> str:
    body = {
        "id": str(uuid.uuid4()),
        "account_id": account_id or fx.account_id,
        "name": f"{name}-{uuid.uuid4()}",
        "beginning_balance_cents": beginning,
        "ending_balance_cents": ending,
    }
    if date_start is not None:
        body["date_start"] = date_start
    r = await client.post("/v1/reconciliations", json=body, headers=_idem())
    assert r.status_code == 201, r.text
    recon_id = r.json()["id"]
    fx.recon_ids.append(recon_id)
    return recon_id


async def _post_txn(client, fx, amount_cents: int) -> str:
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": "WP6 movement",
            "amount_cents": amount_cents,
            "date": SEED_DATE,
            "account_id": fx.account_id,
            "category_id": fx.category_id,
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]
    fx.txn_ids.append(txn_id)
    return txn_id


async def _assign(client, txn_id: str, recon_id) -> None:
    r = await client.put(
        f"/v1/transactions/{txn_id}",
        json={"reconciliation_id": recon_id},
        headers=_idem(),
    )
    assert r.status_code == 200, r.text


async def _recon_row(user_id: str, recon_id: str):
    async with db.pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2",
            recon_id, user_id,
        )


# ---------------------------------------------------------------------------
# The regression the package exists to prevent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_editing_one_reconciliation_never_changes_another(client, fx, test_data):
    """Under chaining, editing A's ending balance rewrote B's beginning balance
    through a cascade with no status predicate — mutating a COMPLETED row the
    field-lock refuses to touch. Now an edit is local to its row: B must be
    byte-identical afterwards, in DRAFT and in COMPLETED status alike.
    """
    a_id = await _create_recon(
        client, fx, name="wp6-A", beginning=0, ending=10_000,
        date_start="2024-01-01T00:00:00Z",
    )
    b_id = await _create_recon(
        client, fx, name="wp6-B", beginning=10_000, ending=20_000,
        date_start="2024-02-01T00:00:00Z",
    )

    # B in DRAFT.
    b_before = await _recon_row(test_data.user_id, b_id)
    r = await client.put(
        f"/v1/reconciliations/{a_id}",
        json={"ending_balance_cents": 99_999},
        headers=_idem(),
    )
    assert r.status_code == 200, r.text
    b_after = await _recon_row(test_data.user_id, b_id)
    assert dict(b_before) == dict(b_after), (
        "editing A mutated draft B: "
        f"{dict(b_before)} -> {dict(b_after)}"
    )

    # B COMPLETED — the status the old cascade rewrote through the back door.
    txn_id = await _post_txn(client, fx, -500)
    await _assign(client, txn_id, b_id)
    r = await client.post(f"/v1/reconciliations/{b_id}/complete", headers=_idem())
    assert r.status_code == 200, r.text

    b_before = await _recon_row(test_data.user_id, b_id)
    r = await client.put(
        f"/v1/reconciliations/{a_id}",
        json={"ending_balance_cents": 123_456, "beginning_balance_cents": 77},
        headers=_idem(),
    )
    assert r.status_code == 200, r.text
    b_after = await _recon_row(test_data.user_id, b_id)
    assert dict(b_before) == dict(b_after), (
        "editing A mutated completed B: "
        f"{dict(b_before)} -> {dict(b_after)}"
    )

    # Unwind so teardown's blanket DELETE isn't hiding a failure above.
    r = await client.post(f"/v1/reconciliations/{b_id}/revert", headers=_idem())
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# The derived mode is gone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_without_beginning_balance_is_rejected(client, fx):
    """No prefill, no chained inference: omitting the value is a 422, not an
    invitation for the engine to derive one."""
    r = await client.post(
        "/v1/reconciliations",
        json={
            "id": str(uuid.uuid4()),
            "account_id": fx.account_id,
            "name": f"wp6-no-begin-{uuid.uuid4()}",
        },
        headers=_idem(),
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_removed_fields_are_rejected_not_ignored(client, fx):
    """Fail closed: the request schemas are extra="forbid", so the deleted
    fields 422 instead of being silently dropped. This is also the root fix
    for bug 5.3 — the old sort_order guard in the PUT was dead code and the
    field passed with a silent 200.
    """
    for extra in ({"sort_order": 3}, {"beginning_balance_source": "chained"}):
        r = await client.post(
            "/v1/reconciliations",
            json={
                "id": str(uuid.uuid4()),
                "account_id": fx.account_id,
                "name": f"wp6-extra-{uuid.uuid4()}",
                "beginning_balance_cents": 0,
                **extra,
            },
            headers=_idem(),
        )
        assert r.status_code == 422, (extra, r.text)

    recon_id = await _create_recon(client, fx, name="wp6-put-extra")
    for extra in ({"sort_order": 3}, {"beginning_balance_source": "manual"}):
        r = await client.put(
            f"/v1/reconciliations/{recon_id}",
            json=extra,
            headers=_idem(),
        )
        assert r.status_code == 422, (extra, r.text)


@pytest.mark.asyncio
async def test_reorder_route_is_gone(client, fx):
    r = await client.put(
        f"/v1/accounts/{fx.account_id}/reconciliations/order",
        json={"ordered_ids": [str(uuid.uuid4())]},
        headers=_idem(),
    )
    assert r.status_code in (404, 405), r.text


# ---------------------------------------------------------------------------
# Ordering is chronological
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_list_orders_by_date_start_nulls_last(client, fx):
    """date_start went from pure label to the thing that orders the list.
    Creation order is deliberately scrambled relative to the dates, and the
    undated row sorts last.
    """
    feb = await _create_recon(
        client, fx, name="wp6-feb", date_start="2024-02-01T00:00:00Z",
    )
    undated = await _create_recon(client, fx, name="wp6-undated")
    jan = await _create_recon(
        client, fx, name="wp6-jan", date_start="2024-01-01T00:00:00Z",
    )

    r = await client.get(f"/v1/reconciliations?account_id={fx.account_id}")
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()["items"]]
    assert ids == [jan, feb, undated], ids


# ---------------------------------------------------------------------------
# difference_cents — computed, never stored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_difference_cents_is_the_unexplained_remainder(client, fx, test_data):
    """begin 100000, end 150000 → the statement claims +50000 happened.
    Assigned: +70000 inflow, −25000 outflow, and a −5000 transfer leg
    (an ordinary row per WP1 — transfer legs are never excluded from sums).
    Signed sum 40000, so 10000 is left to explain.
    """
    recon_id = await _create_recon(
        client, fx, name="wp6-diff", beginning=100_000, ending=150_000,
    )

    # A fresh batch explains nothing: difference = ending − beginning.
    r = await client.get(f"/v1/reconciliations/{recon_id}")
    assert r.status_code == 200, r.text
    assert r.json()["difference_cents"] == 50_000

    inflow = await _post_txn(client, fx, 70_000)
    outflow = await _post_txn(client, fx, -25_000)
    await _assign(client, inflow, recon_id)
    await _assign(client, outflow, recon_id)

    # Transfer out of the reconciled account; the outgoing leg joins the batch.
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": "WP6 transfer",
            "amount_cents": -5_000,
            "date": SEED_DATE,
            "account_id": fx.account_id,
            "transfer": {
                "id": str(uuid.uuid4()),
                "account_id": fx.second_account_id,
                "amount_cents": 5_000,
            },
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    leg_id = r.json()["id"]
    fx.txn_ids.extend([leg_id, r.json()["transfer_transaction_id"]])
    await _assign(client, leg_id, recon_id)

    r = await client.get(f"/v1/reconciliations/{recon_id}")
    assert r.status_code == 200, r.text
    assert r.json()["difference_cents"] == 10_000

    # The figure is on list rows too, and identical there.
    r = await client.get(f"/v1/reconciliations?account_id={fx.account_id}")
    assert r.status_code == 200, r.text
    by_id = {row["id"]: row for row in r.json()["items"]}
    assert by_id[recon_id]["difference_cents"] == 10_000

    # Soft-deleting an assigned transaction removes it from the sum in the
    # same read — nothing is stored, so nothing needs a second write to
    # stay in step. (-25000 leaves the sum → difference drops by 25000.)
    r = await client.delete(f"/v1/transactions/{outflow}", headers=_idem())
    assert r.status_code == 200, r.text
    r = await client.get(f"/v1/reconciliations/{recon_id}")
    assert r.json()["difference_cents"] == -15_000

    # Nothing in the row stores it.
    row = await _recon_row(test_data.user_id, recon_id)
    assert "difference_cents" not in row.keys()


# ---------------------------------------------------------------------------
# The status enum is closed at the database
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_check_constraint_rejects_unknown_values(fx, test_data):
    """sql/025 closes the enum (bug 6.3's reconciliation slice): status is
    NOT NULL, so the CHECK cannot be NULL-skipped, and 3 must not insert."""
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO expense_reconciliations
                    (id, user_id, account_id, name, status,
                     beginning_balance_cents, ending_balance_cents,
                     created_at, updated_at)
                   VALUES ($1, $2, $3, 'wp6-bad-status', 3, 0, 0, now(), now())""",
                str(uuid.uuid4()), test_data.user_id, fx.account_id,
            )


# ---------------------------------------------------------------------------
# Delete audit snapshots — difference_cents on both sides of the mutation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_audit_snapshots_pin_the_unassign_ordering(
    client, fx, test_data
):
    """The DELETED activity entry's before-snapshot carries the batch's
    pre-delete difference (membership included, computed at fetch time);
    the after-snapshot is re-fetched AFTER the cascade unassign, so its
    difference is the emptied batch (ending − beginning). Pins the
    refetch-after-unassign ordering in delete_reconciliation."""
    recon_id = await _create_recon(
        client, fx, name="wp6-audit-diff", beginning=0, ending=10_000,
    )
    txn_id = await _post_txn(client, fx, 4_000)
    await _assign(client, txn_id, recon_id)

    r = await client.get(f"/v1/reconciliations/{recon_id}")
    assert r.json()["difference_cents"] == 6_000

    r = await client.delete(f"/v1/reconciliations/{recon_id}", headers=_idem())
    assert r.status_code == 200, r.text
    assert r.json()["difference_cents"] == 10_000

    async with db.pool.acquire() as conn:
        entry = await conn.fetchrow(
            """SELECT before_snapshot, after_snapshot FROM activity_log
               WHERE resource_id = $1 AND user_id = $2 AND resource_type = 'reconciliation'
               ORDER BY created_at DESC LIMIT 1""",
            recon_id, test_data.user_id,
        )
    before = json.loads(entry["before_snapshot"])
    after = json.loads(entry["after_snapshot"])
    assert before["difference_cents"] == 6_000
    assert after["difference_cents"] == 10_000
    assert after["deleted_at"] is not None
