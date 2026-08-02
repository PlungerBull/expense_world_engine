"""Parity between the SQL conversion fragments and ``get_rate``.

``app/helpers/home_currency.py`` and ``app/helpers/exchange_rate.get_rate``
implement the same carry-forward rule — most recent rate on or before the date —
in two languages. That duplication is deliberate (aggregates must run in SQL;
pulling every row into Python would be worse), but it is also exactly how audit
finding WP1.2 happened: a rule written twice, the copies drifted, and the
surviving copy was the buggy one. This file is the mitigation.

The parity rows assert **agreement**, not absolute values. ``exchange_rates`` has
no ``user_id`` column, so its rows are global and shared across xdist workers; a
concurrent insert by another test file moves both implementations together and
parity still holds, whereas an absolute assertion would flake. The one absolute
assertion (the NULL path) is made at a date below the suite-wide seed floor.

Seeding rules this file obeys, because global rows are shared:

  * Seed strictly inside 2001-01-01 .. 2020-12-31. Below it,
    ``test_phase_fixes`` asserts a 2000-01-01 write gets 422 RATE_UNAVAILABLE and
    a stray earlier row would make that return 201 on another worker. Above it,
    ``conftest`` owns CURRENT_DATE.
  * Delete only our own dates in teardown — never ``DELETE FROM exchange_rates``.
  * Every seeded day gets a *distinct* rate, so "resolved this rate" proves
    "resolved this row".

Rates are compared, not converted cents: ``_fetch_rate_from_db`` truncates the
stored ``numeric`` to a binary float and Python rounds half-to-even, while the SQL
keeps full precision and rounds half-away-from-zero, so the two can legitimately
differ by one cent on an amount. Audit WP1.7 tracks the fix; do not "strengthen"
these into a cents comparison.

Run: .venv/bin/pytest tests/test_home_currency_parity.py -v
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import uuid

import pytest

from app import db
from app.helpers.exchange_rate import clear_rate_cache, get_rate
from app.constants import TransactionType, TransferDirection
from app.helpers.home_currency import (
    ACCOUNT_ALIAS,
    HOME_CENTS_EXPR,
    SIGNED_CENTS_EXPR,
    SIGNED_HOME_CENTS_EXPR,
    TXN_ALIAS,
    UNCONVERTIBLE_FLAG_EXPR,
    home_rate_join,
)

# (rate_date, rate) — inside the permitted window, one distinct rate per day.
# 2010-06-12/13 are a weekend and 2010-06-17 is a deliberate gap: both are
# omitted so the carry-forward path has something to carry.
SEEDED_RATES = [
    ("2010-06-01", "3.01"),
    ("2010-06-11", "3.11"),  # Friday
    ("2010-06-14", "3.14"),  # Monday
    ("2010-06-15", "3.15"),
    ("2010-06-16", "3.16"),
    ("2010-06-18", "3.18"),
]
SEEDED_DATES = [date.fromisoformat(d) for d, _ in SEEDED_RATES]
# asyncpg binds in binary, so date/numeric parameters must arrive as the real
# Python types — a ``$1::date`` cast in the SQL does not rescue a str.
SEEDED_ROWS = [(date.fromisoformat(d), Decimal(r)) for d, r in SEEDED_RATES]

AMOUNT_CENTS = 2500

# The scaffold every caller of these fragments must provide. The accounts join is
# a LEFT JOIN on purpose — see the module docstring in app/helpers/home_currency.
# $1 = transaction id, $2 = display_timezone.
SCAFFOLD = f"""
    SELECT
        ({HOME_CENTS_EXPR})          AS home_cents,
        r.rate                       AS rate,
        ({UNCONVERTIBLE_FLAG_EXPR})  AS flag,
        ({SIGNED_CENTS_EXPR})        AS signed_cents,
        ({SIGNED_HOME_CENTS_EXPR})   AS signed_home_cents,
        {TXN_ALIAS}.amount_cents     AS amount_cents
    FROM expense_transactions {TXN_ALIAS}
    LEFT JOIN expense_bank_accounts {ACCOUNT_ALIAS}
           ON {ACCOUNT_ALIAS}.id = {TXN_ALIAS}.account_id
    {home_rate_join("$2")}
    WHERE {TXN_ALIAS}.id = $1
"""


def _midday(day: str) -> datetime:
    """Noon UTC on `day`.

    Fixture transactions sit at midday so no plausible timezone shifts their
    calendar day. Timezone handling then cannot be what makes a parity row pass
    or fail, and the test measures rate resolution — its actual subject. The
    near-midnight case is pinned separately below.
    """
    return datetime.fromisoformat(f"{day}T12:00:00+00:00")


class Fixtures:
    def __init__(self):
        self.usd_account_id = str(uuid.uuid4())
        self.archived_category_id = str(uuid.uuid4())
        self.txn_ids: list[str] = []


@pytest.fixture
async def fx(test_data, db_pool):
    """Seed rates, a USD account, an empty archived category. Clean up all three."""
    data = Fixtures()

    async with db.pool.acquire() as conn:
        for rate_date, rate in SEEDED_ROWS:
            await conn.execute(
                """INSERT INTO exchange_rates
                    (base_currency, target_currency, rate, rate_date, created_at)
                   VALUES ('USD', 'PEN', $1, $2, now())
                   ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING""",
                rate, rate_date,
            )
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'USD-Parity', 'USD', false, '#00FF00',
                       0, false, 98, now(), now())""",
            data.usd_account_id, test_data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, is_system, is_archived, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, 'Parity Archived', '#00FF00', false, true, 98,
                       now(), now())""",
            data.archived_category_id, test_data.user_id,
        )

    # get_rate caches negative results for an hour, so a date probed before its
    # row was seeded would return a stale None and fail for the wrong reason.
    clear_rate_cache()

    yield data

    async with db.pool.acquire() as conn:
        if data.txn_ids:
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[])",
                data.txn_ids,
            )
        await conn.execute(
            "DELETE FROM expense_bank_accounts WHERE id = $1", data.usd_account_id
        )
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = $1", data.archived_category_id
        )
        # Only our own dates. Other workers share this table.
        await conn.execute(
            """DELETE FROM exchange_rates
               WHERE base_currency = 'USD' AND target_currency = 'PEN'
                 AND rate_date = ANY($1::date[])""",
            SEEDED_DATES,
        )
    clear_rate_cache()


async def _seed_txn(conn, fx: Fixtures, user_id: str, category_id: str,
                    account_id: str, when: datetime,
                    txn_type: int = int(TransactionType.EXPENSE),
                    direction: int = None) -> str:
    """Insert one ledger row, leaving ``amount_home_cents`` NULL.

    NULL on purpose: the whole point of these fragments is that the home value is
    computed, so a regression that reads the stored column would surface here
    instead of quietly agreeing.
    """
    txn_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO expense_transactions
            (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
             transfer_direction, date, account_id, category_id, exchange_rate,
             cleared, created_at, updated_at)
           VALUES ($1, $2, 'parity', $3, NULL, $4,
             $5, $6, $7, $8, 1.0, false, now(), now())""",
        txn_id, user_id, AMOUNT_CENTS, txn_type, direction,
        when, account_id, category_id,
    )
    fx.txn_ids.append(txn_id)
    return txn_id


# ---------------------------------------------------------------------------
# Parity matrix
# ---------------------------------------------------------------------------

# (label, transaction day, expected rate or None for "agreement only")
PARITY_CASES = [
    ("exact row", "2010-06-15", "3.15"),
    ("carry-forward past the last row", "2010-06-20", "3.18"),
    ("weekend", "2010-06-12", "3.11"),
    ("deliberate gap day", "2010-06-17", "3.16"),
    # Before the earliest date this file seeds. Deliberately NOT phrased as
    # "expect NULL": carry-forward matches any earlier row in a global table, and
    # test_exchange_rates_history seeds 1997 rows from another worker. Agreement
    # is the property that holds regardless.
    ("before the seeded range", "2010-05-20", None),
]


@pytest.mark.parametrize(
    "label,day,expected_rate",
    PARITY_CASES,
    ids=[c[0] for c in PARITY_CASES],
)
async def test_sql_and_get_rate_resolve_the_same_row(
    fx, test_data, label, day, expected_rate
):
    async with db.pool.acquire() as conn:
        txn_id = await _seed_txn(
            conn, fx, test_data.user_id, test_data.category_id,
            fx.usd_account_id, _midday(day),
        )
        # One snapshot for both lookups: another worker inserting or deleting a
        # global rate row between them would otherwise break parity spuriously.
        async with conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(SCAFFOLD, txn_id, "UTC")
            # as_of is derived the same way the SQL derives it — the transaction
            # is at midday UTC and the zone above is UTC, so this is that date.
            py = await get_rate(conn, "USD", "PEN", date.fromisoformat(day))

    # Agreement is the assertion, including agreeing that nothing resolves. The
    # "before the seeded range" case genuinely lands either way depending on
    # whether test_exchange_rates_history's 1997 rows are live on another worker;
    # both implementations must simply reach the same verdict.
    assert (row["rate"] is None) == (py is None), (
        f"{label}: SQL resolved {row['rate']!r} but get_rate resolved {py!r}"
    )
    if row["rate"] is not None:
        assert float(row["rate"]) == pytest.approx(py[0]), (
            f"{label}: SQL and get_rate disagree on the rate"
        )
        assert row["flag"] == 0, f"{label}: convertible row must not raise the flag"
        assert row["home_cents"] == round(AMOUNT_CENTS * float(row["rate"])), label
    else:
        assert row["home_cents"] is None and row["flag"] == 1, label

    if expected_rate is not None:
        # Every seeded day has a distinct rate, so this pins the exact row.
        assert float(row["rate"]) == pytest.approx(float(expected_rate)), label


async def test_home_currency_row_needs_no_rate(fx, test_data):
    """PEN -> PEN: asserted on the value, not the rate.

    The fragment leaves ``r.rate`` NULL for a home-currency row and returns
    ``amount_cents`` from the first CASE arm — it never produces a rate of 1.0 the
    way ``get_rate`` does, so there is nothing to compare rates against.
    """
    async with db.pool.acquire() as conn:
        txn_id = await _seed_txn(
            conn, fx, test_data.user_id, test_data.category_id,
            test_data.account_id, _midday("2010-06-15"),
        )
        row = await conn.fetchrow(SCAFFOLD, txn_id, "UTC")

    assert row["rate"] is None
    assert row["home_cents"] == row["amount_cents"] == AMOUNT_CENTS
    assert row["flag"] == 0


# ---------------------------------------------------------------------------
# The sign matrix
# ---------------------------------------------------------------------------

# (label, transaction_type, transfer_direction, expected sign)
SIGN_CASES = [
    ("income", int(TransactionType.INCOME), None, 1),
    ("expense", int(TransactionType.EXPENSE), None, -1),
    ("transfer credit", int(TransactionType.TRANSFER),
     int(TransferDirection.CREDIT), 1),
    ("transfer debit", int(TransactionType.TRANSFER),
     int(TransferDirection.DEBIT), -1),
]


@pytest.mark.parametrize(
    "label,txn_type,direction,sign",
    SIGN_CASES,
    ids=[c[0] for c in SIGN_CASES],
)
async def test_sign_matrix_applies_to_native_and_home_alike(
    fx, test_data, label, txn_type, direction, sign
):
    """Income and transfer credits are inflow; expenses and transfer debits outflow.

    Collapsing three duplicated copies of this matrix into one definition is what
    audit finding WP9.1 asks for, its stated risk being that /dashboard and
    /reports/monthly drift and disagree about the same month. The home form must
    be the native rule applied to the converted magnitude — same sign, same
    classification — which is why both are asserted from one row.
    """
    async with db.pool.acquire() as conn:
        txn_id = await _seed_txn(
            conn, fx, test_data.user_id, test_data.category_id,
            fx.usd_account_id, _midday("2010-06-15"),
            txn_type=txn_type, direction=direction,
        )
        row = await conn.fetchrow(SCAFFOLD, txn_id, "UTC")

    expected_home = round(AMOUNT_CENTS * 3.15)
    assert row["home_cents"] == expected_home, label
    assert row["signed_cents"] == sign * AMOUNT_CENTS, label
    assert row["signed_home_cents"] == sign * expected_home, label
    # The signed form wraps the unsigned one rather than re-deriving it, so the
    # magnitudes must match exactly.
    assert abs(row["signed_home_cents"]) == row["home_cents"], label


# ---------------------------------------------------------------------------
# The timezone decision
# ---------------------------------------------------------------------------

async def test_rate_date_resolves_in_the_users_timezone(fx, test_data):
    """A transaction after UTC midnight but before local midnight prices locally.

    This case is the only one that discriminates. For a UTC user the obvious
    "23:00 resolves the local day" test is vacuous — 23:00Z is the same calendar
    day under AT TIME ZONE 'UTC', under a hardcoded UTC cast, and under the bare
    ``::date`` the explicit cast exists to eliminate, so it would pass against the
    bug it is meant to catch. Both zones are asserted here so the discrimination
    is visible: a hardcoded-UTC cast makes the first assertion fail.

        ('2010-06-16T02:00:00Z' AT TIME ZONE 'UTC')::date          -> 2010-06-16
        ('2010-06-16T02:00:00Z' AT TIME ZONE 'America/Lima')::date -> 2010-06-15

    The zone is supplied as the bind parameter rather than by mutating
    ``user_settings``: nothing imports this module yet, so the test builds the
    query itself and passes exactly what CR2's callers will pass — the value from
    ``get_user_report_settings``.
    """
    just_after_utc_midnight = datetime(2010, 6, 16, 2, 0, tzinfo=timezone.utc)

    async with db.pool.acquire() as conn:
        txn_id = await _seed_txn(
            conn, fx, test_data.user_id, test_data.category_id,
            fx.usd_account_id, just_after_utc_midnight,
        )
        lima = await conn.fetchrow(SCAFFOLD, txn_id, "America/Lima")
        utc = await conn.fetchrow(SCAFFOLD, txn_id, "UTC")

    assert float(lima["rate"]) == pytest.approx(3.15), (
        "UTC-5 puts this instant on 2010-06-15; a hardcoded-UTC cast gives 3.16"
    )
    assert float(utc["rate"]) == pytest.approx(3.16)
    assert lima["home_cents"] != utc["home_cents"]


# ---------------------------------------------------------------------------
# The missing-rate (NULL) path
# ---------------------------------------------------------------------------

async def test_unconvertible_row_is_null_and_flagged(fx, test_data):
    """No rate on or before the date: NULL home value, flag 1, native untouched.

    Asserted absolutely, which is only safe because 1990-01-01 is below the
    suite-wide seed floor — the earliest row any test seeds is
    ``test_exchange_rates_history``'s 1997-01-14. A future test seeding earlier
    than 1990 breaks this one. (Querying below the floor is fine; *seeding* below
    2001 is what the file docstring forbids.)
    """
    async with db.pool.acquire() as conn:
        txn_id = await _seed_txn(
            conn, fx, test_data.user_id, test_data.category_id,
            fx.usd_account_id, _midday("1990-01-01"),
        )
        row = await conn.fetchrow(SCAFFOLD, txn_id, "UTC")
        py = await get_rate(conn, "USD", "PEN", date(1990, 1, 1))

    assert py is None
    assert row["rate"] is None
    assert row["home_cents"] is None, "must be NULL, never coalesced to the native amount"
    assert row["flag"] == 1
    # The NULL must survive the sign matrix as NULL, not collapse to 0 — the
    # aggregation contract exists precisely because SUM then drops it silently.
    assert row["signed_home_cents"] is None
    assert row["amount_cents"] == AMOUNT_CENTS, "native figures are unaffected"
    assert row["signed_cents"] == -AMOUNT_CENTS, "native figures are unaffected"


async def test_empty_category_is_not_flagged_as_unconvertible(fx, test_data):
    """The ``t.id IS NOT NULL`` guard, in the shape that needs it.

    ``routers/dashboard.py``'s archived aggregators LEFT JOIN transactions so that
    categories with none survive with zero totals. On those rows t.* and a.* are
    all NULL and HOME_CENTS_EXPR falls to its ELSE NULL arm — indistinguishable
    from a genuinely unconvertible row. Without the guard every empty archived
    category would report ``spent_home_cents: null`` instead of 0, contradicting
    the invariant the LEFT JOIN exists to preserve.
    """
    query = f"""
        SELECT
            COALESCE(SUM({UNCONVERTIBLE_FLAG_EXPR}), 0)::bigint AS unconvertible,
            COALESCE(SUM({HOME_CENTS_EXPR}), 0)::bigint         AS home_cents
        FROM expense_categories c
        LEFT JOIN expense_transactions {TXN_ALIAS}
               ON {TXN_ALIAS}.category_id = c.id
              AND {TXN_ALIAS}.user_id     = c.user_id
              AND {TXN_ALIAS}.deleted_at IS NULL
        LEFT JOIN expense_bank_accounts {ACCOUNT_ALIAS}
               ON {ACCOUNT_ALIAS}.id = {TXN_ALIAS}.account_id
        {home_rate_join("$2")}
        WHERE c.id = $1
        GROUP BY c.id
    """
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(query, fx.archived_category_id, "UTC")

    assert row is not None, "the LEFT JOIN must preserve the empty category"
    assert row["unconvertible"] == 0, "an empty category is not an unconvertible one"
    assert row["home_cents"] == 0
