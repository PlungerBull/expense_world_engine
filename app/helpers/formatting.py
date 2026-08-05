"""Shared response-formatting helpers.

Consolidates ``apply_debit_as_negative`` which was duplicated in
transactions.py and reconciliations.py routers.

``debit_as_negative`` is a caller-side display preference, never a schema
property — these functions work on a shallow copy and the stored row is
untouched. Both variants read direction from the one channel the ledger and
the inbox share: ``transaction_type``. Transfers need no special case, because
after WP1 a transfer leg is an ordinary outflow or inflow.
"""

from typing import Optional

from app.constants import TransactionType


def _is_debit(transaction_type: Optional[int]) -> bool:
    """Does this row's primary side reduce its account's balance?

    ``None`` type means an inbox row with no amount yet — not a debit, not a
    credit, nothing to flip.
    """
    return transaction_type == TransactionType.OUTFLOW


def apply_debit_as_negative(data: dict) -> dict:
    """Post-process a transaction dict to negate amounts for expenses/debits.

    Returns a shallow copy with ``amount_cents`` negated when the transaction is
    an expense or a transfer-debit. There is one amount to flip: a transaction
    carries no home-currency value (sql/021).
    """
    if not _is_debit(data["transaction_type"]):
        return data

    data = {**data}
    data["amount_cents"] = -data["amount_cents"]
    return data


def apply_debit_as_negative_inbox(data: dict) -> dict:
    """Post-process an inbox dict to negate amounts for expenses/transfer-debits.

    Same direction rule as the ledger variant — an inbox row stores its amounts
    positive and carries ``transaction_type`` exactly as
    ``expense_transactions`` does.

    The one addition is ``transfer_amount_cents``: an inbox row holds both legs
    of a transfer, so the sibling is negated in the *opposite* direction to the
    primary. It used to be emitted as-stored beside a flipped primary, which
    rendered a transfer as two amounts pointing the same way
    (WP10.2, docs/audit-2026-08-01-remediation-plan.md:297).

    There is no longer a transfer branch: ``transaction_type`` does not
    distinguish transfers from ordinary rows, which is the point of WP1. The
    sibling keys are simply ``None`` on an ordinary row, so the per-key
    presence check does the discriminating that ``== TRANSFER`` used to.

    Rows with no amount yet (``transaction_type`` still ``None``) pass through
    unchanged.
    """
    transaction_type = data.get("transaction_type")
    if transaction_type is None:
        return data

    primary_is_debit = _is_debit(transaction_type)
    data = {**data}

    if primary_is_debit:
        if data.get("amount_cents") is not None:
            data["amount_cents"] = -data["amount_cents"]
    else:
        # The sibling always moves the other way. Only reachable on a transfer
        # draft; on an ordinary row this key is absent.
        if data.get("transfer_amount_cents") is not None:
            data["transfer_amount_cents"] = -data["transfer_amount_cents"]

    return data
