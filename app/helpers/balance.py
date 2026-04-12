"""Account balance mutations.

Single source of truth for how a transaction contributes to an account's
``current_balance_cents``. Previously this logic was duplicated across
transactions router (create + update + delete + batch), inbox promote, and
the transfer helper — each copy with its own slightly different control flow.

The sign convention is encoded here once:

    EXPENSE        → subtract amount
    INCOME         → add amount
    TRANSFER+DEBIT → subtract amount
    TRANSFER+CREDIT→ add amount

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

from typing import Optional

import asyncpg

from app.constants import TransactionType, TransferDirection


def _delta_for_apply(
    amount_cents: int,
    transaction_type: int,
    transfer_direction: Optional[int],
) -> Optional[int]:
    """Compute the balance delta for applying a transaction.

    Returns ``None`` if the combination is unrecognised (caller should treat
    as a no-op, matching the pre-refactor behaviour of the private helpers
    in ``routers/transactions.py``).
    """
    if transaction_type == TransactionType.EXPENSE:
        return -amount_cents
    if transaction_type == TransactionType.INCOME:
        return amount_cents
    if transaction_type == TransactionType.TRANSFER:
        if transfer_direction == TransferDirection.DEBIT:
            return -amount_cents
        if transfer_direction == TransferDirection.CREDIT:
            return amount_cents
    return None


async def apply_balance(
    conn: asyncpg.Connection,
    account_id: str,
    user_id: str,
    amount_cents: int,
    transaction_type: int,
    transfer_direction: Optional[int] = None,
) -> None:
    """Apply a transaction's balance contribution to its account.

    ``amount_cents`` is always positive (storage convention). The sign is
    derived from ``transaction_type`` and ``transfer_direction`` per the
    matrix documented at module level.
    """
    delta = _delta_for_apply(amount_cents, transaction_type, transfer_direction)
    if delta is None:
        return
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
    transfer_direction: Optional[int] = None,
) -> None:
    """Reverse a transaction's balance contribution.

    Used when un-applying a transaction (delete, or update that changes
    amount/account before the new values are applied). This is the exact
    negation of ``apply_balance``.
    """
    delta = _delta_for_apply(amount_cents, transaction_type, transfer_direction)
    if delta is None:
        return
    # Reverse sign: what was applied, now un-applied.
    delta = -delta
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
