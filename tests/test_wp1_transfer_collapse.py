"""WP1 — direction lives in ``transaction_type`` on every row.

These tests pin the invariants the transfer collapse exists to establish:

  * every ledger row carries a direction, enforced by the database rather
    than by a comment (``transaction_type IN (1, 2)``, ``NOT NULL``);
  * ``amount_cents`` is always stored positive, likewise enforced;
  * a transfer's two legs cancel;
  * a half-transfer inbox row remains unrepresentable after ``sql/019``'s
    coherence CHECK was rewritten without ``transfer_direction``.

The USD→USD case is open bug 1.3, closed here rather than in WP2 by owner
decision — see ``docs/rework/WP1-transfer-collapse.md`` and the entry in
``docs/client-breaking-changes.md``.
"""
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

from app import db

# Today at midnight UTC. conftest seeds a USD→PEN rate at the database's
# CURRENT_DATE, which in a UTC-negative session timezone is today's UTC date or
# the day before — either way "on or before" this timestamp, so a rate always
# resolves. And midnight UTC today is never in the future, which the create
# path rejects. A hardcoded date would fail the moment the test database is
# re-cloned, since create-test-db.sh copies no exchange_rates rows.
TXN_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


async def _make_account(user_id: str, currency: str, name: str) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, false, '#123456', 0, false, 9, now(), now())
            """,
            account_id, user_id, f"{name}-{account_id[:8]}", currency,
        )
    return account_id


async def _drop_accounts(*account_ids: str) -> None:
    async with db.pool.acquire() as conn:
        for account_id in account_ids:
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1", account_id
            )


async def _drop_transactions(*transaction_ids) -> None:
    async with db.pool.acquire() as conn:
        for transaction_id in transaction_ids:
            if not transaction_id:
                continue
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", str(transaction_id)
            )
            # Break the reciprocal FK before deleting either row.
            await conn.execute(
                "UPDATE expense_transactions SET transfer_transaction_id = NULL WHERE id = $1",
                transaction_id,
            )
        for transaction_id in transaction_ids:
            if not transaction_id:
                continue
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1", transaction_id
            )


# ---------------------------------------------------------------------------
# Open bug 1.3 — every USD→USD transfer returned 500
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_usd_to_usd_transfer_succeeds(client, test_data):
    """A transfer between two USD accounts under a PEN home currency.

    Before WP1 the dominant-side block had no rule for "neither leg matches
    main_currency" and fell to ``raise RuntimeError``, uncaught, 500. WP1
    reordered the block to value the primary at the market rate; WP2 then
    deleted the block outright, so there is no branch left to fall off. The bug
    went from repaired to unrepresentable, and this test still pins it.
    """
    from_id = await _make_account(test_data.user_id, "USD", "wp1-usd-from")
    to_id = await _make_account(test_data.user_id, "USD", "wp1-usd-to")
    primary_id = sibling_id = None
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"usd-to-usd-{uuid.uuid4()}",
                "amount_cents": -50000,
                "date": TXN_DATE,
                "account_id": from_id,
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": to_id,
                    "amount_cents": 50000,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        primary_id = body["id"]
        sibling_id = body["transfer_transaction_id"]
        assert sibling_id is not None
    finally:
        await _drop_transactions(primary_id, sibling_id)
        await _drop_accounts(from_id, to_id)


@pytest.mark.asyncio
async def test_transfer_rejects_a_supplied_exchange_rate(client, test_data):
    """A caller cannot supply an exchange rate on a transfer. There is nowhere
    to put one.

    This test used to assert the opposite half of open bug 1.3: that a PEN
    primary was worth its own amount *whatever* rate the caller sent, because
    the dominant-side block tested the override before the currency-match rule
    and computed ``home = amount × rate`` — 5000 × 3.5 = 17500 for an amount
    that is, definitionally, S/50.00 in soles.

    sql/021 deleted the block, the columns and the request field together, so
    the question stops being "which rate wins" and becomes "why is the client
    sending a rate at all". Fail closed: 422.
    """
    sibling = await _make_account(test_data.user_id, "PEN", "wp1-pen-sibling")
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"pen-with-rate-{uuid.uuid4()}",
                "amount_cents": -5000,
                "date": TXN_DATE,
                "account_id": test_data.account_id,  # PEN, the home currency
                "exchange_rate": 3.5,
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": sibling,
                    "amount_cents": 5000,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        assert "exchange_rate" in (r.json()["error"].get("fields") or {}), r.text
    finally:
        await _drop_accounts(sibling)


# ---------------------------------------------------------------------------
# The two legs cancel
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transfer_legs_cancel(client, test_data):
    """One outflow, one inflow, cancelling in the account's own currency.

    Same-currency, so the home figure says the same thing — and after WP2 it
    says it because both legs converted at the same rate for the same date, not
    because a write-time rule forced them equal. The cross-currency case no
    longer cancels: it reports the FX spread, which
    tests/test_wp2_read_time_currency.py covers.
    """
    sibling = await _make_account(test_data.user_id, "PEN", "wp1-cancel")
    primary_id = sibling_id = None
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"cancel-{uuid.uuid4()}",
                "amount_cents": -7500,
                "date": TXN_DATE,
                "account_id": test_data.account_id,
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": sibling,
                    "amount_cents": 7500,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        primary_id = r.json()["id"]
        sibling_id = r.json()["transfer_transaction_id"]

        async with db.pool.acquire() as conn:
            legs = await conn.fetch(
                """
                SELECT id, transaction_type, amount_cents,
                       transfer_transaction_id
                FROM expense_transactions
                WHERE id = ANY($1::uuid[])
                ORDER BY transaction_type
                """,
                [primary_id, sibling_id],
            )

        assert len(legs) == 2
        outflow, inflow = legs
        assert outflow["transaction_type"] == 1
        assert inflow["transaction_type"] == 2

        # Stored positive on both legs — the sign is in the type, never the value.
        assert outflow["amount_cents"] > 0 and inflow["amount_cents"] > 0

        # Reciprocal pairing is what makes them a transfer at all: after WP1 it
        # is the only thing that does.
        assert str(outflow["transfer_transaction_id"]) == str(inflow["id"])
        assert str(inflow["transfer_transaction_id"]) == str(outflow["id"])

        signed_native = -outflow["amount_cents"] + inflow["amount_cents"]
        assert signed_native == 0
    finally:
        await _drop_transactions(primary_id, sibling_id)
        await _drop_accounts(sibling)


# ---------------------------------------------------------------------------
# Every row has a direction — enforced by the database, not by a comment
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_type,label",
    [(3, "the retired transfer value"), (0, "out of range")],
)
async def test_ledger_rejects_a_transaction_type_outside_the_direction_enum(
    test_data, bad_type, label
):
    """``transaction_type`` is direction, and only direction.

    sql/003 declared this column ``NOT NULL`` and left it open to any smallint
    — that is the ``expense_transactions`` half of open bug 6.3. A row typed 3
    used to mean "transfer", with its real direction in a second column; such a
    row is now unstorable rather than merely unwritten.
    """
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, amount_cents, transaction_type,
                     date, account_id, category_id,
                     created_at, updated_at)
                VALUES ($1, $2, 'bad-type', 1000, $3, now(), $4, $5, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id, bad_type,
                test_data.account_id, test_data.category_id,
            )


# ---------------------------------------------------------------------------
# The read paths still classify a transfer correctly
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transfer_cancels_in_the_monthly_report(client, test_data):
    """@Transfer nets to zero, and the transfer adds volume without moving net.

    This is the surface WP1 rewrote: helpers/monthly_report.py carried two
    literal copies of a four-branch sign matrix keyed on `transaction_type = 3`
    and `transfer_direction`, and both now call
    helpers/home_currency.signed_expr instead. If the collapse mis-signed a
    transfer leg, @Transfer would stop cancelling — which is exactly the
    failure the standing rule "transfers stay visible in reports, never
    excluded from totals" is there to catch.
    """
    sibling = await _make_account(test_data.user_id, "PEN", "wp1-report")
    when = datetime.now(timezone.utc)
    params = {"year": when.year, "month": when.month}
    primary_id = sibling_id = None
    try:
        r = await client.get("/v1/reports/monthly", params=params)
        assert r.status_code == 200, r.text
        before = r.json()["totals"]

        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"report-transfer-{uuid.uuid4()}",
                "amount_cents": -7500,
                "date": TXN_DATE,
                "account_id": test_data.account_id,
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": sibling,
                    "amount_cents": 7500,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        primary_id = r.json()["id"]
        sibling_id = r.json()["transfer_transaction_id"]

        r = await client.get("/v1/reports/monthly", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        after = body["totals"]

        transfer_cat = [c for c in body["categories"] if c["name"] == "@Transfer"]
        assert transfer_cat, "the engine must have created @Transfer"
        assert transfer_cat[0]["spent_home_cents"] == 0, "the two legs must cancel"
        assert transfer_cat[0]["unconverted_count"] == 0

        # Both legs are counted — gross volume moves, net does not.
        assert after["outflow_home_cents"] == before["outflow_home_cents"] + 7500
        assert after["inflow_home_cents"] == before["inflow_home_cents"] + 7500
        assert after["net_home_cents"] == before["net_home_cents"]
    finally:
        await _drop_transactions(primary_id, sibling_id)
        await _drop_accounts(sibling)


@pytest.mark.asyncio
async def test_ledger_rejects_a_negative_amount(test_data):
    """``amount_cents`` is stored positive — now a database fact, not a habit.

    The ledger's counterpart to sql/019's ``inbox_amount_positive``. With
    direction in a typed column on every row, a negative stored amount would be
    a second, contradictory encoding of the same fact.
    """
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, amount_cents, transaction_type,
                     date, account_id, category_id,
                     created_at, updated_at)
                VALUES ($1, $2, 'negative', -1000, 1, now(), $3, $4, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id,
                test_data.account_id, test_data.category_id,
            )
