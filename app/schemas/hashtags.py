from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.schemas import StrictModel, audit_fields, owned_fields


class HashtagCreateRequest(StrictModel):
    id: UUID
    name: str
    sort_order: Optional[int] = None


class HashtagUpdateRequest(StrictModel):
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


def hashtag_from_row(row) -> dict:
    return HashtagResponse(
        **owned_fields(row),
        name=row["name"],
        sort_order=row["sort_order"],
        **audit_fields(row),
    ).model_dump(mode="json")
