from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class HashtagCreateRequest(BaseModel):
    name: str
    sort_order: Optional[int] = None


class HashtagUpdateRequest(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None


class HashtagResponse(BaseModel):
    id: str
    user_id: str
    name: str
    sort_order: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None
