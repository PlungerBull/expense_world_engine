"""Shared response-formatting helpers.

``debit_as_negative`` is a caller-side display preference, never a schema
property — the function works on a shallow copy and the stored row is
untouched. Direction is read from the one channel the ledger and the inbox
share: ``transaction_type``.

One implementation serves both surfaces since the transfer removal
(2026-08-10): the inbox-specific variant existed only to negate the
sibling ``transfer_amount_cents`` a transfer draft carried, and that
column is gone. An inbox draft with no amount yet has ``transaction_type
is None`` and passes through unchanged; when the type is set, the inbox
invariant "type non-null ⇒ amount non-null" holds (the type is only ever
derived from a present amount), so the subscript below cannot miss.
"""

from typing import Optional

from app.constants import TransactionType


def _is_debit(transaction_type: Optional[int]) -> bool:
    """Does this row reduce its account's balance?

    ``None`` type means an inbox row with no amount yet — not a debit, not a
    credit, nothing to flip.
    """
    return transaction_type == TransactionType.OUTFLOW


def apply_debit_as_negative(data: dict) -> dict:
    """Post-process a transaction or inbox dict, negating debit amounts.

    Returns a shallow copy with ``amount_cents`` negated when the row is an
    outflow. There is one amount to flip: a row carries no home-currency
    value (sql/021).
    """
    if not _is_debit(data.get("transaction_type")):
        return data

    data = {**data}
    data["amount_cents"] = -data["amount_cents"]
    return data
