"""Domain constants for the expense engine.

Using IntEnum so these are backwards-compatible with existing integer
comparisons (e.g. ``transaction_type == TransactionType.OUTFLOW``) while
still providing readable ``repr()`` output in logs and debuggers.
"""

from enum import Enum, IntEnum


# The one home currency the engine converts to. Locked at the schema level by
# sql/018 (CHECK main_currency = 'PEN'), so this is a constant rather than a
# per-user value. It exists because SQL fragments in helpers/home_currency.py
# interpolate the home currency as a literal — see that module's docstring for
# why binding it is not an option, and for what has to change if sql/018 is
# ever reverted.
HOME_CURRENCY = "PEN"

# The canonical base of every stored exchange-rate row: rates are stored as
# (base_currency=BASE_CURRENCY, target_currency=X, rate = units of X per 1 base
# unit); direction math lives in helpers/exchange_rate.get_rate. sql/015 locks
# the engine's currency set to {USD, PEN}, which is what makes interpolating
# this into SQL literals safe (same argument as HOME_CURRENCY above). The
# currency-lock docs say base and home must move together on any unlock —
# naming both here is what makes that greppable. NOT every 'USD' literal is
# this constant: helpers/home_currency's `a.currency_code = 'USD'` guard means
# "the only supported non-home currency" and deliberately stays a literal (see
# the fail-closed comment beside it).
BASE_CURRENCY = "USD"


class SystemCategoryKey(str, Enum):
    """Stable discriminator for engine-managed categories.

    Stored in ``expense_categories.system_key``. The display ``name`` can be
    renamed by the user freely; the engine identifies the category by this
    immutable key so transfer pairs always resolve to the same row.
    """
    DEBT = "debt"
    TRANSFER = "transfer"
    OPENING_BALANCE = "opening_balance"


# Default display name for each system category key when the row is
# first seeded. Users are free to rename afterwards; the engine never
# reads the display name to locate a system row.
SYSTEM_CATEGORY_DEFAULT_NAMES: dict[SystemCategoryKey, str] = {
    SystemCategoryKey.DEBT: "@Debt",
    SystemCategoryKey.TRANSFER: "@Transfer",
    SystemCategoryKey.OPENING_BALANCE: "@Opening",
}


class TransactionType(IntEnum):
    """Which way money moved on this row's account. Nothing else.

    Present on every ledger row, never null, CHECK-enforced by sql/020.
    There is deliberately no ``TRANSFER`` member: a transfer is two ordinary
    rows, one OUTFLOW and one INFLOW, paired by ``transfer_transaction_id``.
    That column is the discriminator — "the counterparty is an account you
    also own" is a fact about the pairing, not about the direction.

    The names are OUTFLOW/INFLOW rather than EXPENSE/INCOME because these
    values type transfer legs too, and a transfer's outgoing leg is not an
    expense. sql/020's header records why the two facts were separated.
    """
    OUTFLOW = 1
    INFLOW = 2


class TransactionSource(IntEnum):
    """Which writer attached this hashtag junction row.

    ``expense_transaction_hashtags.transaction_source`` — CHECK-locked by
    sql/027 to exactly this one value. There is deliberately no ``INBOX = 2``
    member: the value exists nowhere in code, and sql/027's header instructs
    that the member and the widened CHECK ship *with* the inbox-hashtag
    writer, never ahead of it — an admissible-but-unwritten value is how
    half-copied conventions become load-bearing by accident.
    """
    LEDGER = 1


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
