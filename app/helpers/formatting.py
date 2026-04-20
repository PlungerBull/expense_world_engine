"""Shared response-formatting helpers.

Consolidates ``apply_debit_as_negative`` which was duplicated in
transactions.py and reconciliations.py routers.
"""

from app.constants import TransactionType, TransferDirection


def apply_debit_as_negative(data: dict) -> dict:
    """Post-process a transaction dict to negate amounts for expenses/debits.

    Returns a shallow copy with ``amount_cents`` and ``amount_home_cents``
    negated when the transaction is an expense or a transfer-debit.
    """
    t = data["transaction_type"]
    d = data.get("transfer_direction")
    if t == TransactionType.EXPENSE or (t == TransactionType.TRANSFER and d == TransferDirection.DEBIT):
        data = {**data}
        data["amount_cents"] = -data["amount_cents"]
        if data["amount_home_cents"] is not None:
            data["amount_home_cents"] = -data["amount_home_cents"]
    return data


def apply_debit_as_negative_inbox(data: dict) -> dict:
    """Post-process an inbox dict to negate amounts for expenses/transfer-outflows.

    Inbox rows store ``amount_cents`` positive and carry direction on two
    channels: ``transaction_type`` for regular rows (EXPENSE vs INCOME), and
    the sign of ``transfer_amount_cents`` for transfer rows (positive means
    the sibling is receiving, so the primary is the outflow leg).

    Returns a shallow copy with ``amount_cents`` and ``amount_home_cents``
    negated when the primary side is a debit. Untyped inbox rows (amount
    and type both ``None``) pass through unchanged.
    """
    t = data.get("transaction_type")
    if t is None:
        return data

    should_negate = False
    if t == TransactionType.EXPENSE:
        should_negate = True
    elif t == TransactionType.TRANSFER:
        transfer_amount_cents = data.get("transfer_amount_cents")
        if transfer_amount_cents is not None and transfer_amount_cents > 0:
            should_negate = True

    if not should_negate:
        return data

    data = {**data}
    if data.get("amount_cents") is not None:
        data["amount_cents"] = -data["amount_cents"]
    if data.get("amount_home_cents") is not None:
        data["amount_home_cents"] = -data["amount_home_cents"]
    return data
