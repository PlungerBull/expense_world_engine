from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class HashtagCreateRequest(BaseModel):
    id: UUID
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


def hashtag_from_row(row) -> dict:
    return HashtagResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")
