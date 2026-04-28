from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.constants import BeginningBalanceSource
from app.schemas.transactions import TransactionResponse


# Wire labels for beginning_balance_source. The DB column is a smallint
# (1=manual, 2=chained per BeginningBalanceSource); the API surface uses
# strings so clients don't pin to internal magic numbers.
SOURCE_LABEL_BY_INT: dict[int, str] = {
    BeginningBalanceSource.MANUAL: "manual",
    BeginningBalanceSource.CHAINED: "chained",
}
SOURCE_INT_BY_LABEL: dict[str, int] = {v: k for k, v in SOURCE_LABEL_BY_INT.items()}


class ReconciliationCreateRequest(BaseModel):
    id: UUID
    account_id: str
    name: str
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    # Provided => source becomes "manual" and value is stored verbatim.
    # Omitted => source becomes "chained" and value is derived from the
    # previous neighbor in sort_order (defaulting to 0 if none).
    beginning_balance_cents: Optional[int] = None
    ending_balance_cents: Optional[int] = None
    # Insert at this position (existing rows at >= sort_order shift +1).
    # Omitted => append (max(sort_order)+1 for the account).
    sort_order: Optional[int] = None


class ReconciliationUpdateRequest(BaseModel):
    name: Optional[str] = None
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    beginning_balance_cents: Optional[int] = None
    ending_balance_cents: Optional[int] = None
    # Toggle source explicitly. Setting "manual" freezes the current
    # value. Setting "chained" re-derives from the current previous
    # neighbor (or leaves the value alone if none exists). Sending
    # beginning_balance_cents in the same body always wins and forces
    # source to "manual".
    beginning_balance_source: Optional[Literal["manual", "chained"]] = None
    # sort_order is intentionally NOT here. The dedicated reorder endpoint
    # is the only path that mutates it. The router rejects sort_order in
    # request bodies via extra="forbid" / explicit guard.


class ReconciliationResponse(BaseModel):
    id: str
    user_id: str
    account_id: str
    name: str
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    status: int
    sort_order: int
    beginning_balance_cents: int
    beginning_balance_home_cents: Optional[int] = None
    beginning_balance_source: Literal["manual", "chained"]
    chained_from_reconciliation_id: Optional[str] = None
    ending_balance_cents: int
    ending_balance_home_cents: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def reconciliation_from_row(
    row,
    rate: Optional[float] = None,
    chained_from_reconciliation_id: Optional[str] = None,
) -> dict:
    """Serialize a reconciliation row.

    ``rate`` is the account's currency → user's home currency conversion
    factor at the reconciliation's ``date_end`` (or ``now()`` if not set).
    When ``rate`` is None (e.g. main_currency not configured or no rate
    row available), both home-cents fields serialize as ``null`` so the
    response shape stays stable.

    ``chained_from_reconciliation_id`` is the UUID of the previous neighbor
    in sort_order when the row's source is ``chained`` and a neighbor exists.
    Always ``None`` for ``manual`` rows. Computed by the helper layer
    (requires a per-row neighbor lookup); pass ``None`` to opt out.
    """
    begin = row["beginning_balance_cents"]
    end = row["ending_balance_cents"]
    source_int = row["beginning_balance_source"]
    source_label = SOURCE_LABEL_BY_INT.get(source_int, "manual")
    return ReconciliationResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        account_id=str(row["account_id"]),
        name=row["name"],
        date_start=row["date_start"],
        date_end=row["date_end"],
        status=row["status"],
        sort_order=row["sort_order"],
        beginning_balance_cents=begin,
        beginning_balance_home_cents=round(begin * rate) if rate is not None else None,
        beginning_balance_source=source_label,
        chained_from_reconciliation_id=chained_from_reconciliation_id,
        ending_balance_cents=end,
        ending_balance_home_cents=round(end * rate) if rate is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


class ReconciliationDetailResponse(ReconciliationResponse):
    """Reconciliation plus a paged window of its assigned transactions —
    returned by GET /reconciliations/{id}.

    Validated via a proper Pydantic schema so the response shape is documented
    in OpenAPI and every field follows null-over-omission semantics.

    The embedded list is paginated via ``limit`` / ``offset`` query params
    on the endpoint; ``transactions_total`` / ``transactions_limit`` /
    ``transactions_offset`` echo the window and ``transactions_truncated``
    is True whenever more rows exist beyond the current page.
    """
    transactions: list[TransactionResponse]
    transactions_total: int
    transactions_limit: int
    transactions_offset: int
    transactions_truncated: bool


class ReconciliationReorderRequest(BaseModel):
    """Body for PUT /accounts/{account_id}/reconciliations/order.

    The array is the desired final order for the rows it lists. Engine
    reuses the sort_order slots currently held by the submitted rows
    (sorted ASC) and reassigns them in the new order; rows not in the
    array are untouched.
    """
    ordered_ids: list[UUID] = Field(..., min_length=1)


class ReconciliationReorderResponse(BaseModel):
    """Response shape from the reorder endpoint."""
    reconciliations: list[ReconciliationResponse]
    recalculated_count: int
