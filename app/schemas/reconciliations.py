from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas.transactions import TransactionResponse


class ReconciliationCreateRequest(BaseModel):
    account_id: str
    name: str
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    beginning_balance_cents: Optional[int] = None  # auto-prefilled if omitted
    ending_balance_cents: Optional[int] = None


class ReconciliationUpdateRequest(BaseModel):
    name: Optional[str] = None
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    beginning_balance_cents: Optional[int] = None
    ending_balance_cents: Optional[int] = None


class ReconciliationResponse(BaseModel):
    id: str
    user_id: str
    account_id: str
    name: str
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    status: int
    beginning_balance_cents: int
    ending_balance_cents: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def reconciliation_from_row(row) -> dict:
    return ReconciliationResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        account_id=str(row["account_id"]),
        name=row["name"],
        date_start=row["date_start"],
        date_end=row["date_end"],
        status=row["status"],
        beginning_balance_cents=row["beginning_balance_cents"],
        ending_balance_cents=row["ending_balance_cents"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


class ReconciliationDetailResponse(ReconciliationResponse):
    """Reconciliation plus its assigned transactions — returned by GET /reconciliations/{id}.

    Validated via a proper Pydantic schema so the response shape is documented
    in OpenAPI and every field follows null-over-omission semantics.
    """
    transactions: list[TransactionResponse]
    transactions_truncated: bool  # True if the transactions list was capped
