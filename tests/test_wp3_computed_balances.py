"""WP3 — an account's balance is computed from the ledger, never stored.

sql/022 dropped ``expense_bank_accounts.current_balance_cents`` and deleted
``app/helpers/balance.py`` with its eleven mutation sites. The balance is now the
signed sum of the account's non-deleted transactions, in the account's own
currency (``app/helpers/account_balance.py``).

What that buys and what it costs:

  * Nothing can drift. A stored balance had two sources of truth and every write
    path had to keep them in step; a sum has one. The tests below that soft-delete
    and restore rows are asserting exactly this — under the old model each of
    those paths carried its own reversal call, and any one of them could be
    forgotten.
  * The @Opening seed becomes load-bearing. It used to be one input among many,
    and a wrong one could hide behind the cached figure. It is now the FIRST TERM
    of the sum: wrong or missing, and the account is wrong by that amount forever,
    on every screen. Pinned below.
  * The report exclusion must not leak in. @Opening is excluded from flow reports
    and INCLUDED in balances. Those two facts live one function apart, and
    ``helpers/monthly_report`` is the natural template to copy a CTE from, so the
    test that holds them apart is the point of this file rather than a detail.

Seeding rules this file obeys
-----------------------------

Every account, category and transaction here is created inline and torn down in a
``finally``. This file seeds NO exchange rates and asserts no home-currency value:
balances are native by definition, and the home conversion is WP2's
(``test_wp2_read_time_currency.py`` owns 2022, ``test_home_currency_parity`` owns
2001-2020, ``test_exchange_rates_history`` owns 1997, ``conftest`` owns
CURRENT_DATE). Dates below are deliberately recent-but-past so they land in no
other file's window and are never in the future.

The conftest account is read but never mutated — under xdist that row is shared,
and its computed balance (-5000, from conftest's single seeded outflow) is an
input to the query-count test only.
"""
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app import db
from app.helpers.account_balance import fetch_balance, fetch_balances


# Far enough back to sit outside the current month, so the monthly report's
# window never accidentally includes or excludes these rows for the wrong reason.
SEED_DATE = "2024-02-15T12:00:00Z"


class Fixtures:
    def __init__(self):
        self.account_id = str(uuid.uuid4())
        self.second_account_id = str(uuid.uuid4())
        self.category_id = str(uuid.uuid4())
        self.txn_ids: list[str] = []


@pytest.fixture
async def fx(test_data, db_pool):
    """Two PEN accounts and a private category, all torn down afterwards."""
    data = Fixtures()

    async with db.pool.acquire() as conn:
        for account_id, name in (
            (data.account_id, "WP3-Primary"),
            (data.second_account_id, "WP3-Secondary"),
        ):
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at)
                   VALUES ($1, $2, $3, 'PEN', false, '#5522AA',
                           false, 96, now(), now())""",
                account_id, test_data.user_id, f"{name}-{account_id[:8]}",
            )
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, is_system, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, $3, '#5522AA', false, 96, now(), now())""",
            data.category_id, test_data.user_id, f"WP3-Cat-{data.category_id[:8]}",
        )

    yield data

    async with db.pool.acquire() as conn:
        account_ids = [data.account_id, data.second_account_id]
        await conn.execute(
            """DELETE FROM activity_log
               WHERE user_id = $1 AND resource_id = ANY($2::uuid[])""",
            test_data.user_id, account_ids + data.txn_ids,
        )
        # Both legs of a transfer point at each other, so neither can go first.
        await conn.execute(
            """DELETE FROM expense_transactions
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


async def _balance(user_id: str, account_id: str) -> int:
    async with db.pool.acquire() as conn:
        return await fetch_balance(conn, user_id, account_id)


async def _post_txn(client, fx, amount_cents: int, date: str = SEED_DATE) -> str:
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": "WP3 movement",
            "amount_cents": amount_cents,
            "date": date,
            "account_id": fx.account_id,
            "category_id": fx.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]
    fx.txn_ids.append(txn_id)
    return txn_id


# ---------------------------------------------------------------------------
# The sum itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_account_with_no_transactions_reports_zero_not_null(client, fx, test_data):
    """Zero is a balance, not a missing value.

    The join direction is the thing under test: an account absent from the
    ledger's ``GROUP BY`` must still appear with 0. Get it backwards and the
    account vanishes from the list, or serializes ``null`` into a field the wire
    contract declares non-nullable.
    """
    assert await _balance(test_data.user_id, fx.account_id) == 0

    r = await client.get(f"/v1/accounts/{fx.account_id}")
    assert r.status_code == 200, r.text
    assert r.json()["current_balance_cents"] == 0


@pytest.mark.asyncio
async def test_balance_equals_opening_plus_movements(client, fx, test_data):
    """The invariant the whole package exists to make honest.

    Opening 100000, then -2500 and +700. The opening seed is the first term, not
    a special case: it is an ordinary transaction row and the sum treats it as
    one.
    """
    r = await client.post(
        f"/v1/accounts/{fx.account_id}/opening-balance",
        json={
            "transaction_id": str(uuid.uuid4()),
            "amount_cents": 100000,
            "date": SEED_DATE,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    fx.txn_ids.append(r.json()["id"])

    assert await _balance(test_data.user_id, fx.account_id) == 100000

    await _post_txn(client, fx, -2500)
    await _post_txn(client, fx, 700)

    assert await _balance(test_data.user_id, fx.account_id) == 100000 - 2500 + 700

    r = await client.get(f"/v1/accounts/{fx.account_id}")
    assert r.json()["current_balance_cents"] == 98200


@pytest.mark.asyncio
async def test_soft_delete_removes_a_movement_and_restore_puts_it_back(client, fx, test_data):
    """Delete → restore is a round trip, with no reversal step to forget.

    Under the stored column this needed ``reverse_balance`` on the delete path and
    ``apply_balance`` on the restore path, each a separate write that could go
    missing. Now ``deleted_at`` is the only thing that moves, and the sum already
    filters on it.
    """
    await _post_txn(client, fx, -1000)
    txn_id = await _post_txn(client, fx, -4000)

    assert await _balance(test_data.user_id, fx.account_id) == -5000

    r = await client.delete(
        f"/v1/transactions/{txn_id}",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    assert await _balance(test_data.user_id, fx.account_id) == -1000

    r = await client.post(
        f"/v1/transactions/{txn_id}/restore",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    assert await _balance(test_data.user_id, fx.account_id) == -5000


@pytest.mark.asyncio
async def test_moving_a_transaction_between_accounts_moves_the_money(client, fx, test_data):
    """One UPDATE, two balances.

    This is the path the deleted ``needs_balance_update`` flag guarded: a PUT
    changing ``account_id`` had to reverse on the old account and apply on the
    new one, and the flag had to be kept in step with every field that might
    matter. Re-pointing the row now does both halves at once.
    """
    txn_id = await _post_txn(client, fx, -3000)

    assert await _balance(test_data.user_id, fx.account_id) == -3000
    assert await _balance(test_data.user_id, fx.second_account_id) == 0

    r = await client.put(
        f"/v1/transactions/{txn_id}",
        json={"account_id": fx.second_account_id},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text

    assert await _balance(test_data.user_id, fx.account_id) == 0
    assert await _balance(test_data.user_id, fx.second_account_id) == -3000


@pytest.mark.asyncio
async def test_a_transfer_moves_both_accounts_and_nets_to_zero(client, fx, test_data):
    """Both legs are ordinary rows, so both balances move by construction."""
    before_primary = await _balance(test_data.user_id, fx.account_id)
    before_secondary = await _balance(test_data.user_id, fx.second_account_id)

    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": "WP3 transfer",
            "amount_cents": -7500,
            "date": SEED_DATE,
            "account_id": fx.account_id,
            "transfer": {
                "id": str(uuid.uuid4()),
                "account_id": fx.second_account_id,
                "amount_cents": 7500,
            },
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    fx.txn_ids.append(body["id"])

    after_primary = await _balance(test_data.user_id, fx.account_id)
    after_secondary = await _balance(test_data.user_id, fx.second_account_id)

    assert after_primary == before_primary - 7500
    assert after_secondary == before_secondary + 7500
    assert (after_primary - before_primary) + (after_secondary - before_secondary) == 0


@pytest.mark.asyncio
async def test_an_archived_account_still_reports_its_balance(client, fx, test_data):
    """Archiving hides an account from pickers. It does not spend the money."""
    await _post_txn(client, fx, -8800)

    r = await client.post(
        f"/v1/accounts/{fx.account_id}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_balance_cents"] == -8800
    assert r.json()["is_archived"] is True

    r = await client.get("/v1/accounts?include_archived=true")
    assert r.status_code == 200, r.text
    row = next(a for a in r.json()["items"] if a["id"] == fx.account_id)
    assert row["current_balance_cents"] == -8800


# ---------------------------------------------------------------------------
# @Opening: in the balance, out of the report
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opening_counts_toward_the_balance_but_not_the_month_report(
    client, fx, test_data
):
    """The one place two rules about @Opening meet, and must not be merged.

    ``helpers/monthly_report`` carries a ``NOT EXISTS (... system_key =
    'opening_balance')`` filter in both of its CTEs. The balance sum must NOT
    inherit it — an opening balance is where tracking starts (so it is not flow)
    while still being money you have (so it is balance). Copying that CTE
    wholesale is the mistake this test exists to catch.
    """
    now = datetime.now(timezone.utc)
    # Yesterday, so it lands in a month the dashboard will report on while
    # never being a future-dated row (which the engine rejects).
    recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")

    r = await client.post(
        f"/v1/accounts/{fx.account_id}/opening-balance",
        json={
            "transaction_id": str(uuid.uuid4()),
            "amount_cents": 50000,
            "date": recent,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    fx.txn_ids.append(r.json()["id"])

    # In the balance.
    assert await _balance(test_data.user_id, fx.account_id) == 50000

    # Out of the flow report: the seed contributes nothing to this month's
    # inflow, and the @Opening category row is not listed.
    r = await client.get("/v1/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()

    account_row = next(a for a in body["bank_accounts"] if a["id"] == fx.account_id)
    assert account_row["current_balance_cents"] == 50000

    category_ids = {c["id"] for c in body["categories"]}
    async with db.pool.acquire() as conn:
        opening_category_id = await conn.fetchval(
            """SELECT id FROM expense_categories
               WHERE user_id = $1 AND system_key = 'opening_balance'
                 AND deleted_at IS NULL""",
            test_data.user_id,
        )
    assert opening_category_id is not None
    assert str(opening_category_id) not in category_ids


# ---------------------------------------------------------------------------
# The properties that make the deletion safe rather than merely correct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_query_count_does_not_grow_with_the_number_of_accounts(
    client, fx, test_data
):
    """The N+1 guard, and the honest half of WP3's EXPLAIN requirement.

    An ``EXPLAIN``-asserting test would be theatre here: the test database holds
    a handful of rows, so the planner picks a sequential scan whichever indexes
    exist. The measured plans live in sql/022's header, captured against 50,000
    rows. What IS testable, deterministically and at any size, is the shape:
    listing accounts must cost the same number of round trips for five accounts
    as for one. That is the defect the requirement was guarding against.
    """
    statements: list[str] = []
    original = asyncpg.Connection.fetch
    original_row = asyncpg.Connection.fetchrow
    original_val = asyncpg.Connection.fetchval

    async def counting(kind, orig):
        async def wrapper(self, query, *args, **kwargs):
            statements.append(f"{kind}:{query[:40]}")
            return await orig(self, query, *args, **kwargs)
        return wrapper

    asyncpg.Connection.fetch = await counting("fetch", original)
    asyncpg.Connection.fetchrow = await counting("fetchrow", original_row)
    asyncpg.Connection.fetchval = await counting("fetchval", original_val)
    try:
        statements.clear()
        r = await client.get("/v1/accounts?limit=200")
        assert r.status_code == 200, r.text
        accounts_with_two = len(statements)

        statements.clear()
        r = await client.get("/v1/dashboard?include_archived=true")
        assert r.status_code == 200, r.text
        dashboard_with_two = len(statements)

        # Add three more accounts, each with a transaction, then measure again.
        extra_ids = [str(uuid.uuid4()) for _ in range(3)]
        async with db.pool.acquire() as conn:
            for i, account_id in enumerate(extra_ids):
                await conn.execute(
                    """INSERT INTO expense_bank_accounts
                        (id, user_id, name, currency_code, is_person, color,
                         is_archived, sort_order, created_at, updated_at)
                       VALUES ($1, $2, $3, 'PEN', false, '#5522AA',
                               false, 96, now(), now())""",
                    account_id, test_data.user_id, f"WP3-N1-{account_id[:8]}",
                )
                await conn.execute(
                    """INSERT INTO expense_transactions
                        (id, user_id, title, amount_cents, transaction_type, date,
                         account_id, category_id, cleared, created_at, updated_at)
                       VALUES ($1, $2, 'n+1 probe', $3, 1, $4, $5, $6, false,
                               now(), now())""",
                    str(uuid.uuid4()), test_data.user_id, (i + 1) * 100,
                    datetime.fromisoformat(SEED_DATE.replace("Z", "+00:00")),
                    account_id, fx.category_id,
                )

        try:
            statements.clear()
            r = await client.get("/v1/accounts?limit=200")
            assert r.status_code == 200, r.text
            accounts_with_five = len(statements)

            statements.clear()
            r = await client.get("/v1/dashboard?include_archived=true")
            assert r.status_code == 200, r.text
            dashboard_with_five = len(statements)

            assert accounts_with_five == accounts_with_two, (
                f"GET /accounts query count grew with account count: "
                f"{accounts_with_two} → {accounts_with_five}. Statements: {statements}"
            )
            # The dashboard renders three account panels, each of which reads its
            # own slice's balances in one query. Three panels is a constant; the
            # number of accounts in them is not.
            assert dashboard_with_five == dashboard_with_two, (
                f"GET /dashboard query count grew with account count: "
                f"{dashboard_with_two} → {dashboard_with_five}. Statements: {statements}"
            )
        finally:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    """DELETE FROM expense_transactions
                       WHERE user_id = $1 AND account_id = ANY($2::uuid[])""",
                    test_data.user_id, extra_ids,
                )
                await conn.execute(
                    """DELETE FROM expense_bank_accounts
                       WHERE id = ANY($1::uuid[]) AND user_id = $2""",
                    extra_ids, test_data.user_id,
                )
    finally:
        asyncpg.Connection.fetch = original
        asyncpg.Connection.fetchrow = original_row
        asyncpg.Connection.fetchval = original_val


@pytest.mark.asyncio
async def test_fetch_balances_raises_rather_than_defaulting_an_unasked_account(
    fx, test_data
):
    """A forgotten account must be a KeyError, never a plausible zero.

    ``fetch_balances`` seeds every id it was ASKED about with 0, so a real
    empty account reads 0. An id nobody asked about is absent, so
    ``balances[id]`` raises. The distinction matters because a wrong zero on a
    balance is indistinguishable from an empty account — it is the fail-open
    shape ``CLAUDE.md`` forbids, and ``.get(id, 0)`` is how it would get written.
    """
    async with db.pool.acquire() as conn:
        balances = await fetch_balances(conn, test_data.user_id, [fx.account_id])

    assert balances[fx.account_id] == 0  # asked about, has no rows
    with pytest.raises(KeyError):
        balances[fx.second_account_id]  # never asked about


@pytest.mark.asyncio
async def test_the_stored_balance_column_is_gone_from_the_schema(db_pool):
    """Pins the migration itself, not just the code that stopped using it.

    ``deploy/local/create-test-db.sh`` clones the LIVE schema rather than
    replaying ``sql/``. So re-running it with ``--force`` before applying
    sql/022 produces a test database that still has the column — and because
    ``current_balance_cents`` carried ``DEFAULT 0``, every INSERT in the suite
    would still succeed and every test would still pass, against the wrong
    schema. This is the assertion that makes that loud instead of silent.
    """
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.UndefinedColumnError):
            await conn.fetchval(
                "SELECT current_balance_cents FROM expense_bank_accounts LIMIT 1"
            )
