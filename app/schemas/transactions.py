from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TransferField(BaseModel):
    account_id: str
    amount_cents: int  # signed: negative=outflow, positive=inflow


class TransactionCreateRequest(BaseModel):
    title: str
    amount_cents: int  # signed: negative=expense, positive=income
    date: datetime
    account_id: str
    category_id: str
    description: Optional[str] = None
    exchange_rate: Optional[float] = None
    cleared: Optional[bool] = None
    hashtag_ids: Optional[list[str]] = None
    transfer: Optional[TransferField] = None


class TransactionUpdateRequest(BaseModel):
    title: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    description: Optional[str] = None
    exchange_rate: Optional[float] = None
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
    amount_cents: int
    amount_home_cents: Optional[int] = None
    transaction_type: int
    transfer_direction: Optional[int] = None
    date: datetime
    account_id: str
    category_id: str
    exchange_rate: float
    cleared: bool
    transfer_transaction_id: Optional[str] = None
    parent_transaction_id: Optional[str] = None
    inbox_id: Optional[str] = None
    reconciliation_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def transaction_from_row(row) -> dict:
    return TransactionResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=row["title"],
        description=row["description"],
        amount_cents=row["amount_cents"],
        amount_home_cents=row["amount_home_cents"],
        transaction_type=row["transaction_type"],
        transfer_direction=row["transfer_direction"],
        date=row["date"],
        account_id=str(row["account_id"]),
        category_id=str(row["category_id"]),
        exchange_rate=float(row["exchange_rate"]),
        cleared=row["cleared"],
        transfer_transaction_id=str(row["transfer_transaction_id"]) if row["transfer_transaction_id"] else None,
        parent_transaction_id=str(row["parent_transaction_id"]) if row["parent_transaction_id"] else None,
        inbox_id=str(row["inbox_id"]) if row["inbox_id"] else None,
        reconciliation_id=str(row["reconciliation_id"]) if row["reconciliation_id"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


def infer_transaction_type(amount_cents: int) -> int:
    """Infer transaction_type from signed amount. Negative=expense(1), positive=income(2)."""
    return 1 if amount_cents < 0 else 2
