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


