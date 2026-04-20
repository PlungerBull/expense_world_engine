from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AccountCreateRequest(BaseModel):
    # Reject unknown fields (including is_person) with 422 — person accounts
    # are created via the dedicated People API, never through this endpoint.
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    currency_code: str
    color: Optional[str] = None
    sort_order: Optional[int] = None


class AccountUpdateRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None
    currency_code: Optional[str] = None  # accepted but rejected at service level


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


def account_from_row(row, balance_home_cents: Optional[int] = None) -> dict:
    return AccountResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        currency_code=row["currency_code"],
        is_person=row["is_person"],
        color=row["color"],
        current_balance_cents=row["current_balance_cents"],
        current_balance_home_cents=balance_home_cents,
        is_archived=row["is_archived"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")
