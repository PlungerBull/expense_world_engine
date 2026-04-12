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
