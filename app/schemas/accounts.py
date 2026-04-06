from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AccountCreateRequest(BaseModel):
    name: str
    currency_code: str
    color: Optional[str] = None
    sort_order: Optional[int] = None


class AccountUpdateRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None
    currency_code: Optional[str] = None  # accepted but rejected at router level


class AccountResponse(BaseModel):
    id: str
    user_id: str
    name: str
    currency_code: str
    is_person: bool
    color: str
    current_balance_cents: int
    current_balance_home_cents: Optional[int]
    is_archived: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None
