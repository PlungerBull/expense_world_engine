"""Domain constants for the expense engine.

Using IntEnum so these are backwards-compatible with existing integer
comparisons (e.g. ``transaction_type == TransactionType.EXPENSE``) while
still providing readable ``repr()`` output in logs and debuggers.
"""

from enum import Enum, IntEnum


class SystemCategoryKey(str, Enum):
    """Stable discriminator for engine-managed categories.

    Stored in ``expense_categories.system_key``. The display ``name`` can be
    renamed by the user freely; the engine identifies the category by this
    immutable key so transfer pairs always resolve to the same row.
    """
    DEBT = "debt"
    TRANSFER = "transfer"


# Default display name for each system category key when the row is
# first seeded. Users are free to rename afterwards; the engine never
# reads the display name to locate a system row.
SYSTEM_CATEGORY_DEFAULT_NAMES: dict[SystemCategoryKey, str] = {
    SystemCategoryKey.DEBT: "@Debt",
    SystemCategoryKey.TRANSFER: "@Transfer",
}


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
    RESTORED = 4


class ReconciliationStatus(IntEnum):
    DRAFT = 1
    COMPLETED = 2


class InboxStatus(IntEnum):
    PENDING = 1
    PROMOTED = 2
