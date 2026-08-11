from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import AwareDatetime, BaseModel

from app.constants import InboxStatus, TransactionType
from app.schemas import StrictModel, audit_fields, opt_id, owned_fields


class InboxCreateRequest(StrictModel):
    # Unknown fields 422. The inbox is loose about WHICH fields are null, never
    # about what a field means — and `exchange_rate` no longer means anything
    # (sql/021), nor does `transfer` (removed 2026-08-10), so accepting either
    # silently would be the looser of the two.
    id: UUID
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[AwareDatetime] = None
    # UUID-typed like `id` — malformed FKs 422 at the boundary (open-bugs 6.6).
    account_id: Optional[UUID] = None
    category_id: Optional[UUID] = None


class InboxUpdateRequest(StrictModel):
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[AwareDatetime] = None
    account_id: Optional[UUID] = None
    category_id: Optional[UUID] = None


class InboxPromoteRequest(StrictModel):
    id: UUID  # target ledger transaction id


class InboxResponse(BaseModel):
    id: str
    user_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    # Native only, exactly as on a ledger row. A draft that reported a home
    # value would be reporting a conversion nobody had asked for, at a rate
    # frozen before the draft even had a date — which is precisely how a $100
    # receipt used to promote as 100 PEN cents.
    amount_cents: Optional[int] = None
    # Direction. Nullable: a sparse draft with no amount yet has no direction.
    # Enum-typed like the ledger twin; sql/020 CHECKs the column (with an
    # explicit IS NULL arm).
    transaction_type: Optional[TransactionType] = None
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    # PENDING or PROMOTED — a dismissed row is PENDING + deleted_at, never a
    # third value. sql/029 CHECKs the column.
    status: InboxStatus
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def inbox_from_row(row) -> dict:
    return InboxResponse(
        **owned_fields(row),
        title=row["title"],
        description=row["description"],
        amount_cents=row["amount_cents"],
        transaction_type=row["transaction_type"],
        date=row["date"],
        account_id=opt_id(row["account_id"]),
        category_id=opt_id(row["category_id"]),
        status=row["status"],
        **audit_fields(row),
    ).model_dump(mode="json")
