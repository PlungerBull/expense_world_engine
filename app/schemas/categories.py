from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CategoryCreateRequest(BaseModel):
    name: str
    color: str
    sort_order: Optional[int] = None


class CategoryUpdateRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


class CategoryResponse(BaseModel):
    id: str
    user_id: str
    name: str
    color: str
    is_system: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None
