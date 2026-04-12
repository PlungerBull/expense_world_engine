"""Domain constants for the expense engine.

Using IntEnum so these are backwards-compatible with existing integer
comparisons (e.g. ``transaction_type == TransactionType.EXPENSE``) while
still providing readable ``repr()`` output in logs and debuggers.
"""

from enum import IntEnum


class TransactionType(IntEnum):
    EXPENSE = 1
    INCOME = 2
    TRANSFER = 3


class TransferDirection(IntEnum):
    DEBIT = 1
    CREDIT = 2


class ActivityAction(IntEnum):
    CREATED = 1
    UPDATED = 2
    DELETED = 3


class ReconciliationStatus(IntEnum):
    DRAFT = 1
    COMPLETED = 2


class InboxStatus(IntEnum):
    PENDING = 1
    PROMOTED = 2
