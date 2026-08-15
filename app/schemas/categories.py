from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.schemas import StrictModel, audit_fields, owned_fields


class CategoryCreateRequest(StrictModel):
    id: UUID
    name: str
    color: str
    sort_order: Optional[int] = None


class CategoryUpdateRequest(StrictModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


class CategoryResponse(BaseModel):
    id: str
    user_id: str
    name: str
    color: str
    # Derived, not stored (sql/034): true iff system_key is non-null. Kept on
    # the wire so clients don't each re-derive the delete/assign guard.
    is_system: bool
    # Immutable discriminator ('opening_balance'), null for user categories.
    # Not an IDs-only violation: it is identity, not a hydrated copy of the
    # renameable display name.
    system_key: Optional[str] = None
    sort_order: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def category_from_row(row) -> dict:
    return CategoryResponse(
        **owned_fields(row),
        name=row["name"],
        color=row["color"],
        is_system=row["system_key"] is not None,
        system_key=row["system_key"],
        sort_order=row["sort_order"],
        **audit_fields(row),
    ).model_dump(mode="json")
