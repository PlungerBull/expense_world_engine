from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.schemas.transactions import TransferField


class InboxCreateRequest(BaseModel):
    id: UUID
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    exchange_rate: Optional[float] = None
    transfer: Optional[TransferField] = None


class InboxUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    exchange_rate: Optional[float] = None
    transfer: Optional[TransferField] = None


class InboxPromoteRequest(BaseModel):
    id: UUID  # target ledger transaction id (primary leg for transfer promotes)
    transfer_id: Optional[UUID] = None  # sibling ledger transaction id for transfer promotes


class InboxResponse(BaseModel):
    id: str
    user_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None
    amount_home_cents: Optional[int] = None
    transaction_type: Optional[int] = None
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    exchange_rate: float
    status: int
    transfer_account_id: Optional[str] = None
    transfer_amount_cents: Optional[int] = None
    transfer_amount_home_cents: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def inbox_from_row(row) -> dict:
    rate = float(row["exchange_rate"])
    amount_cents = row["amount_cents"]
    transfer_amount_cents = row["transfer_amount_cents"]
    return InboxResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=row["title"],
        description=row["description"],
        amount_cents=amount_cents,
        amount_home_cents=round(amount_cents * rate) if amount_cents is not None else None,
        transaction_type=row["transaction_type"],
        date=row["date"],
        account_id=str(row["account_id"]) if row["account_id"] else None,
        category_id=str(row["category_id"]) if row["category_id"] else None,
        exchange_rate=rate,
        status=row["status"],
        transfer_account_id=str(row["transfer_account_id"]) if row["transfer_account_id"] else None,
        transfer_amount_cents=transfer_amount_cents,
        transfer_amount_home_cents=(
            round(transfer_amount_cents * rate) if transfer_amount_cents is not None else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")
