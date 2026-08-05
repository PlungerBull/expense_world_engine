"""Account balance mutations.

Single source of truth for how a transaction contributes to an account's
``current_balance_cents``. Previously this logic was duplicated across
transactions router (create + update + delete + batch), inbox promote, and
the transfer helper — each copy with its own slightly different control flow.

The sign convention is encoded here once:

    OUTFLOW → subtract amount
    INFLOW  → add amount

That is the whole matrix. Until WP1 it had four rows, because transfers
carried their direction in a second column (``transfer_direction``) that was
meaningful only when ``transaction_type`` held one specific value. sql/020
collapsed the two columns into one, so a transfer leg is now an ordinary
outflow or inflow and needs no branch of its own.

``reverse_*`` is the exact negation of ``apply_*`` and is used when
un-applying a transaction (update that changes amount/account, delete,
transfer sibling cleanup).

## Transaction boundaries and locks

These functions do NOT open their own ``conn.transaction()`` and do NOT
acquire row-level locks. They assume the caller is already inside a
transaction block and has already acquired any ``FOR UPDATE`` locks it
needs — typically on the transaction row being modified, not on the
account row. See the race-condition fix in ``routers/transactions.py``
update/delete handlers for the lock pattern.

The UPDATE itself (``balance + $delta``) is atomic within a single SQL
statement, so two concurrent inserts on the same account compose
correctly without an explicit account-row lock. The hazard is in
update/delete flows where the caller reads an old ``amount_cents`` and
computes a delta from it — those flows lock the TRANSACTION row so the
amount it reads is stable.
"""

import asyncpg

from app.constants import TransactionType


def _delta_for_apply(amount_cents: int, transaction_type: int) -> int:
    """Compute the balance delta for applying a transaction.

    Raises ``ValueError`` on an unrecognised type. It used to return ``None``
    and let both callers silently no-op, which meant a row whose direction the
    engine could not read moved no balance and said nothing — the exact silent
    drop WP1 removes. ``sql/020``'s ``CHECK (transaction_type IN (1, 2))``
    makes this branch unreachable for stored rows, so reaching it is a bug in
    the caller, not bad data.
    """
    if transaction_type == TransactionType.OUTFLOW:
        return -amount_cents
    if transaction_type == TransactionType.INFLOW:
        return amount_cents
    raise ValueError(
        f"Unknown transaction_type {transaction_type!r}: expected "
        f"{int(TransactionType.OUTFLOW)} (outflow) or "
        f"{int(TransactionType.INFLOW)} (inflow)."
    )


async def apply_balance(
    conn: asyncpg.Connection,
    account_id: str,
    user_id: str,
    amount_cents: int,
    transaction_type: int,
) -> None:
    """Apply a transaction's balance contribution to its account.

    ``amount_cents`` is always positive (storage convention). The sign is
    derived from ``transaction_type`` per the matrix documented at module
    level.
    """
    delta = _delta_for_apply(amount_cents, transaction_type)
    await conn.execute(
        """
        UPDATE expense_bank_accounts
        SET current_balance_cents = current_balance_cents + $1,
            updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        """,
        delta,
        account_id,
        user_id,
    )


async def reverse_balance(
    conn: asyncpg.Connection,
    account_id: str,
    user_id: str,
    amount_cents: int,
    transaction_type: int,
) -> None:
    """Reverse a transaction's balance contribution.

    Used when un-applying a transaction (delete, or update that changes
    amount/account before the new values are applied). This is the exact
    negation of ``apply_balance``.
    """
    # Reverse sign: what was applied, now un-applied.
    delta = -_delta_for_apply(amount_cents, transaction_type)
    await conn.execute(
        """
        UPDATE expense_bank_accounts
        SET current_balance_cents = current_balance_cents + $1,
            updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        """,
        delta,
        account_id,
        user_id,
    )
