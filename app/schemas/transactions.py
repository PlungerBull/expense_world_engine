from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.constants import TransactionType


class TransferField(BaseModel):
    id: UUID  # sibling transaction's client-supplied uuid
    account_id: str
    amount_cents: int  # signed: negative=outflow, positive=inflow


class TransactionCreateRequest(BaseModel):
    # Unknown fields 422 rather than being silently dropped. This is what makes
    # the removal of `exchange_rate` (sql/021) visible to a client still sending
    # it: the engine no longer stores a rate anywhere, and a caller who believes
    # the value matters deserves to be told it does not.
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    amount_cents: int  # signed: negative=expense, positive=income
    date: AwareDatetime
    account_id: str
    # Required for normal transactions, ignored for transfers (the engine
    # auto-assigns @Transfer/@Debt). Conditional requirement is enforced in
    # create_transaction, not here, since it depends on the transfer field.
    category_id: Optional[str] = None
    description: Optional[str] = None
    cleared: Optional[bool] = None
    hashtag_ids: Optional[list[str]] = None
    transfer: Optional[TransferField] = None


class TransactionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[AwareDatetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    description: Optional[str] = None
    cleared: Optional[bool] = None
    hashtag_ids: Optional[list[str]] = None
    reconciliation_id: Optional[str] = None


class TransactionBatchRequest(BaseModel):
    transactions: list[TransactionCreateRequest]


class TransactionResponse(BaseModel):
    id: str
    user_id: str
    title: str
    description: Optional[str] = None
    # Native only. A transaction belongs to one account and the account governs
    # the currency, so a home-currency figure on the row would be a second copy
    # of a number nothing here combines. Conversion happens where currencies are
    # combined — the monthly report and its totals — and nowhere else.
    # Absent, not null: a permanently-null key on every transaction forever is
    # dead weight, and this is the documented exception to null-over-omission.
    amount_cents: int
    # 1 = outflow, 2 = inflow. Direction, and nothing else — a transfer is
    # identified by transfer_transaction_id, not by a third type value.
    transaction_type: int
    date: datetime
    account_id: str
    category_id: str
    cleared: bool
    transfer_transaction_id: Optional[str] = None
    parent_transaction_id: Optional[str] = None
    inbox_id: Optional[str] = None
    reconciliation_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None
    # Per api-design-principles.md §3a — junction tables are storage,
    # the wire format flattens to an embedded array on every transaction
    # representation returned by any read endpoint.
    hashtag_ids: list[str] = Field(default_factory=list)


def transaction_from_row(row, hashtag_ids: Optional[list[str]] = None) -> dict:
    """Serialize a transaction row.

    ``hashtag_ids`` is the resolved set of hashtag UUIDs to attach to the
    response. Callers that surface this dict on the wire — or persist it
    as an activity-log snapshot — MUST pass the actual list (see §3a /
    §6 aggregate exception #1). When omitted, the field defaults to ``[]``.

    The row may also carry a pre-aggregated ``hashtag_ids`` column (this
    is how ``/sync`` already supplies the array via in-query ``array_agg``).
    An explicit ``hashtag_ids`` argument takes precedence over a column
    of the same name on the row.
    """
    resolved: list[str]
    if hashtag_ids is not None:
        resolved = [str(h) for h in hashtag_ids]
    else:
        try:
            row_value = row["hashtag_ids"]
        except (KeyError, TypeError):
            row_value = None
        resolved = [str(h) for h in row_value] if row_value else []

    return TransactionResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=row["title"],
        description=row["description"],
        amount_cents=row["amount_cents"],
        transaction_type=row["transaction_type"],
        date=row["date"],
        account_id=str(row["account_id"]),
        category_id=str(row["category_id"]),
        cleared=row["cleared"],
        transfer_transaction_id=str(row["transfer_transaction_id"]) if row["transfer_transaction_id"] else None,
        parent_transaction_id=str(row["parent_transaction_id"]) if row["parent_transaction_id"] else None,
        inbox_id=str(row["inbox_id"]) if row["inbox_id"] else None,
        reconciliation_id=str(row["reconciliation_id"]) if row["reconciliation_id"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
        hashtag_ids=resolved,
    ).model_dump(mode="json")


def infer_transaction_type(amount_cents: int) -> TransactionType:
    """Infer direction from a signed request amount.

    Negative = OUTFLOW (money leaves the account), positive = INFLOW.

    **This is the only place in the engine a sign is read.** Every write path
    — ordinary transactions, batch, both transfer legs, and the inbox — routes
    through here, so there is exactly one answer to "what does the sign mean".
    Adding a second reader is the bug, not the fix.

    Until WP1 there was a second, byte-identical copy of this rule named
    ``infer_transfer_direction``, because transfers encoded their direction in
    a separate column. That column is gone (sql/020) and so is the duplicate.

    Callers must reject zero first; ``0`` maps to INFLOW here.
    """
    return TransactionType.OUTFLOW if amount_cents < 0 else TransactionType.INFLOW
