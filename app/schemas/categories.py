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


def category_from_row(row) -> dict:
    return CategoryResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        color=row["color"],
        is_system=row["is_system"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")
