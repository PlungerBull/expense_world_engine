from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas.transactions import TransferField


class InboxCreateRequest(BaseModel):
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
    transfer_account_id: Optional[str] = None
    transfer_amount_cents: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


