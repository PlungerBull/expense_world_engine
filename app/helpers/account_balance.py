"""Account balances, computed from the ledger at read time.

An account's balance is the signed sum of its non-deleted transactions, in the
account's own currency. Nothing stores it, so nothing can forget to update it.

**Balances are per account and are never added together here.** Every query below
is ``GROUP BY account_id``, and an account holds exactly one currency, immutable
after creation — so each sum stays inside one currency by construction. Adding a
PEN balance to a USD one would produce a number in no currency at all, which is
the mistake ``CLAUDE.md``'s home-currency rule exists to prevent. The only figure
that spans currencies is ``current_balance_home_cents``, which converts each
account separately before anything is combined — ``fetch_home_balance`` /
``fetch_home_balances`` below, the single implementation since the bloat-audit
§15 consolidation (2026-08-08) replaced three drifting copies (the
``helpers/accounts.get_home_balance`` N+1 path plus two hand-rolled batch loops
in the accounts and dashboard routers).

This module replaces ``app/helpers/balance.py``, which owned the write side of a
stored ``expense_bank_accounts.current_balance_cents`` column (dropped by
sql/022) and its eleven mutation sites. That column was a derived value with a
second source of truth -- the same defect sql/021 removed for currency. The wire
field is unchanged; only its source moved.

The sign matrix is NOT rewritten here. ``home_currency.SIGNED_CENTS_EXPR`` is
the single rendering of it in the engine, and it references only the ``t`` alias,
so the balance sum needs no account join and no rate lateral. Adding a second
copy is the bug, not the fix -- four literal copies drifting is what audit
finding WP9.1 was about, and the deleted ``balance._delta_for_apply`` had already
grown a third copy by hand inside ``transactions.create_batch``.


Why balances are read by explicit account id
--------------------------------------------

``fetch_balances`` requires the ids it is being asked about and pre-seeds every
one of them with 0, so callers index with ``balances[account_id]`` rather than
``balances.get(account_id, 0)``. That is deliberate: ``.get(id, 0)`` is exactly
the expression that emits a confident, wrong zero for an account somebody forgot
to ask about, and a wrong zero on a balance is indistinguishable from an empty
account. A forgotten id raises ``KeyError`` instead.

There is deliberately **no "all accounts" variant**. Every caller already knows
which accounts it is rendering — the account list has its page, each dashboard
panel has its slice — so a ledger-wide scan would be
doing more work to produce a less safe result. It would also have to hand back a
mapping with accounts missing (those with no rows), which forces a
``.get(id, 0)`` at the call site: the fail-open shape this module exists to
avoid.


@Opening is included. The report's exclusion must not be copied here.
---------------------------------------------------------------------

``helpers/monthly_report`` excludes transactions under the ``opening_balance``
system category from flow figures -- an opening balance is where tracking starts,
not money that moved. Balances are the opposite case: the @Opening seed is the
*first term* of the sum. The exclusion lives only in monthly_report's own SQL and
is not attached to ``SIGNED_CENTS_EXPR``, so nothing is inherited by accident --
but monthly_report is the natural template to crib a CTE from, and its
``NOT EXISTS (... system_key = 'opening_balance')`` block sits in the middle of
it. The WHERE clause below is the whole filter: user, and not deleted.

With a stored balance the opening entry was one input among many and a wrong one
could hide behind the cached figure. It is now the seed of the sum: if it is
wrong or missing, the account is wrong by exactly that amount, forever, on every
screen. That is an improvement -- the invariant was always true and is now
honest -- but it is why tests/test_wp3_computed_balances.py pins it.


An obligation for whoever ships split transactions
--------------------------------------------------

The ``parent_transaction_id`` placeholder column was dropped by ``sql/024`` --
it was never written, so this sum is correct today. The documented split rule
is that a parent row is a display container that does not move the balance and
only its children do -- which a naive SUM has no way to honour, so the day
splits ship (with whatever column that fresh design uses), the sum needs a
parent-excluding predicate. The shape that was worked out for the old column
is preserved in ``sql/022``'s header: exclude rows that HAVE children (NOT
EXISTS over the child FK), not rows with a parent -- the inverse keeps parents
and drops children, precisely backwards.


Transaction boundaries
----------------------

These are reads and take no locks. Callers inside ``run_idempotent`` see them in
the same transaction as their own writes; callers on GET paths run outside any
transaction, which is what the account queries beside them already do. A future
caller that genuinely needs a shared snapshot must call from inside its own
REPEATABLE READ block -- see ``helpers/idempotency.run_idempotent`` for the
boundary convention.
"""

from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

import asyncpg

from app.helpers.exchange_rate import batch_get_rates, get_rate, rate_lookup_date
from app.helpers.home_currency import SIGNED_CENTS_EXPR
from app.helpers.settings import get_user_report_settings

# Built once at import, matching helpers/monthly_report.py's handling of the same
# fragments. SIGNED_CENTS_EXPR is `signed_expr("t.amount_cents")` -- the native,
# unconverted signed amount. Balances are always in the account's own currency;
# home-currency balances are converted from this figure at today's rate
# (fetch_home_balance / fetch_home_balances below).
_BALANCE_SUM_SQL = f"SUM({SIGNED_CENTS_EXPR})::bigint"


async def fetch_balances(
    conn: asyncpg.Connection,
    user_id: str,
    account_ids: Iterable[str],
) -> dict[str, int]:
    """Return ``{account_id: balance_cents}`` for exactly the ids requested.

    Every requested id is present in the result, defaulting to 0 -- an account
    with no transactions has a balance of zero, not a missing entry and not
    ``null``. Ids that are not the user's own resolve to 0 rather than leaking
    the existence of another tenant's row.

    One query regardless of how many ids are asked for.
    """
    ids = [str(a) for a in account_ids]
    balances = {account_id: 0 for account_id in ids}
    if not ids:
        return balances

    rows = await conn.fetch(
        f"""
        SELECT t.account_id, {_BALANCE_SUM_SQL} AS balance_cents
        FROM expense_transactions t
        WHERE t.user_id = $1
          AND t.deleted_at IS NULL
          AND t.account_id = ANY($2::uuid[])
        GROUP BY t.account_id
        """,
        user_id,
        ids,
    )
    for row in rows:
        balances[str(row["account_id"])] = int(row["balance_cents"])
    return balances


async def fetch_balance(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> int:
    """Balance for one account. Convenience wrapper over ``fetch_balances``.

    Used by the single-account mutation paths, where the account row is being
    written and a join cannot be spliced onto ``RETURNING *``.
    """
    balances = await fetch_balances(conn, user_id, [account_id])
    return balances[str(account_id)]


def _to_home_cents(balance_cents: int, rate: Decimal) -> int:
    """Convert native cents at ``rate``, rounding the way Postgres does.

    **The one place this module turns a rate into money**, so that the two
    callers below cannot drift — the same reason ``home_currency.signed_expr``
    and ``infer_transaction_type`` are each written once.

    ``ROUND_HALF_UP`` is not a style preference, it is the only mode that
    agrees with SQL. Postgres ``round(numeric)`` is half-away-from-zero, while
    Python's builtin ``round()`` is half-to-even — and, the trap this fix
    exists for, ``round()`` stays half-to-even on a ``Decimal`` too, so
    switching the rate's type without also replacing the call fixes nothing
    (bug 1.7-round). At rate 3.3373 a $50.00 balance is 16687 in SQL and 16686
    under ``round()``, whether the rate is a float or a Decimal.

    Balances reach here already summed, so this rounds once per account — the
    error was never cumulative, only ever a single cent, and now it is none.
    """
    return int((Decimal(balance_cents) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))


async def fetch_home_balances(
    conn: asyncpg.Connection,
    *,
    main_currency: str,
    today: date_type,
    balances: dict[str, int],
    currency_by_id: dict[str, str],
) -> dict[str, Optional[int]]:
    """Convert already-fetched balances to home currency, one rate per currency.

    ``main_currency`` and ``today`` are caller-supplied — the dashboard reads
    settings and the clock ONCE and converts every account slice with them,
    so re-deriving either here would triple the settings read and reopen the
    per-slice midnight drift its comment guards against. Get them from
    ``settings.get_user_report_settings`` (which 422s when settings are
    missing and asserts the home-currency lock) + ``rate_lookup_date``.

    ``None`` means "no rate available for this account's currency today" —
    wire-visible and distinct from a zero balance.
    """
    currencies = set(currency_by_id.values())
    rate_by_currency = (
        await batch_get_rates(conn, currencies, main_currency, today)
        if currencies
        else {}
    )
    result: dict[str, Optional[int]] = {}
    for account_id, balance_cents in balances.items():
        rate = rate_by_currency.get(currency_by_id[account_id])
        result[account_id] = _to_home_cents(balance_cents, rate) if rate is not None else None
    return result


async def fetch_home_balance(
    conn: asyncpg.Connection,
    user_id: str,
    currency_code: str,
    balance_cents: int,
) -> Optional[int]:
    """Home-currency value of one already-known balance, or None if no rate.

    The single-account twin of ``fetch_home_balances`` for the mutation paths
    and the account detail read, which each convert exactly one figure. Takes
    the balance rather than reading it so ``create_account`` can pass its
    by-construction 0 without a wasted ledger query (0 converts to 0, but "no rate for this currency" is still ``null``, and that
    distinction is wire-visible).

    Reads settings itself — and therefore 422s SETTINGS_MISSING like every
    other home-converting surface (owner decision 2026-08-08; before the §15
    consolidation the account routes silently emitted null instead).
    """
    settings = await get_user_report_settings(conn, user_id)
    today = rate_lookup_date(settings["display_timezone"])
    result = await get_rate(
        conn,
        from_currency=currency_code,
        to_currency=settings["main_currency"],
        as_of=today,
    )
    if result is None:
        return None
    return _to_home_cents(balance_cents, result[0])


__all__ = [
    "fetch_balance",
    "fetch_balances",
    "fetch_home_balance",
    "fetch_home_balances",
]
