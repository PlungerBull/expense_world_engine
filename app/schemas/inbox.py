from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class InboxCreateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    exchange_rate: Optional[float] = None


class InboxUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    exchange_rate: Optional[float] = None


class InboxResponse(BaseModel):
    id: str
    user_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    amount_cents: Optional[int] = None
    transaction_type: Optional[int] = None
    date: Optional[datetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    exchange_rate: float
    status: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


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
