"""Account balances, computed from the ledger at read time.

An account's balance is the signed sum of its non-deleted transactions, in the
account's own currency. Nothing stores it, so nothing can forget to update it.

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

"Every account" is a separately named function rather than a ``None`` sentinel,
so scanning the whole ledger is always a decision somebody wrote down.


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

``parent_transaction_id`` is reserved and always null, so this sum is correct
today. The documented split rule is that a parent row is a display container that
does not move the balance and only its children do. Under the stored column that
was enforced by not calling ``apply_balance`` for a parent; a SUM has no such
escape hatch, so the day splits ship, parent and children double-count. The
predicate needed then::

    AND NOT EXISTS (SELECT 1 FROM expense_transactions c
                    WHERE c.parent_transaction_id = t.id
                      AND c.deleted_at IS NULL)

It is NOT ``parent_transaction_id IS NULL``, which excludes the children and
keeps the parents -- precisely backwards. Not added today: no row can have a
parent and the predicate would be unindexed. Also recorded in sql/022.


Transaction boundaries
----------------------

These are reads and take no locks. Callers inside ``run_idempotent`` see them in
the same transaction as their own writes; callers on GET paths run outside any
transaction, which is what the account queries beside them already do. The one
caller that genuinely needs a shared snapshot is ``routers/sync``, and it must
call from inside its REPEATABLE READ block -- see
``helpers/idempotency.run_idempotent`` for the boundary convention.
"""

from typing import Iterable

import asyncpg

from app.helpers.home_currency import SIGNED_CENTS_EXPR

# Built once at import, matching helpers/monthly_report.py's handling of the same
# fragments. SIGNED_CENTS_EXPR is `signed_expr("t.amount_cents")` -- the native,
# unconverted signed amount. Balances are always in the account's own currency;
# home-currency balances are converted from this figure by the caller, at today's
# rate (see helpers/accounts.get_home_balance).
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


async def fetch_all_balances(
    conn: asyncpg.Connection,
    user_id: str,
) -> dict[str, int]:
    """Return ``{account_id: balance_cents}`` for every account with activity.

    Accounts with no transactions are absent from this mapping, because the
    ledger is the only thing read -- callers that hold an account list must
    default those to 0. ``fetch_balances`` is the safer shape and should be
    preferred wherever the ids are already known; this exists for the read paths
    that page or panel over accounts and want one scan for all of them rather
    than one per panel.

    This deliberately scans the user's whole ledger. There is no selective
    predicate to exploit -- summing every account means reading every row -- and
    Postgres correctly picks a sequential scan. Measured at 6 ms for 50,000
    transactions in sql/022's header.
    """
    rows = await conn.fetch(
        f"""
        SELECT t.account_id, {_BALANCE_SUM_SQL} AS balance_cents
        FROM expense_transactions t
        WHERE t.user_id = $1
          AND t.deleted_at IS NULL
        GROUP BY t.account_id
        """,
        user_id,
    )
    return {str(row["account_id"]): int(row["balance_cents"]) for row in rows}


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


def balance_for(balances: dict[str, int], account_id: str) -> int:
    """Look up one account in a mapping from ``fetch_all_balances``.

    Only for the ``fetch_all_balances`` shape, where an account with no
    transactions is legitimately absent. Do not use it on a ``fetch_balances``
    result -- there a missing key means the caller forgot to ask, which must
    raise rather than resolve to a plausible zero.
    """
    return balances.get(str(account_id), 0)


__all__ = [
    "fetch_balance",
    "fetch_balances",
    "fetch_all_balances",
    "balance_for",
]
