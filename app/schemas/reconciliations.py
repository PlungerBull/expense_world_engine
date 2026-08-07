from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.schemas import StrictModel
from app.schemas.transactions import TransactionResponse


class ReconciliationCreateRequest(StrictModel):
    id: UUID
    account_id: str
    name: str
    date_start: Optional[datetime] = None
    date_end: Optional[datetime] = None
    # Required. A beginning balance is a fact the user reads off a statement;
    # the engine never derives one. The former "chained" mode (omit the value,
    # inherit the previous reconciliation's ending balance, recompute on every
    # upstream edit) let a draft edit rewrite a COMPLETED row's balance through
    # the back door and was deleted (sql/025, WP6 of the deletion program).
    beginning_balance_cents: int
    ending_balance_cents: Optional[int] = None


class ReconciliationUpdateRequest(StrictModel):
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
    # Native only. A reconciliation is scoped to ONE account, and the account
    # governs the currency — so there is nothing here to combine and nothing to
    # convert. The two `*_home_cents` fields that used to sit beside these were
    # the last surviving per-account home values, kept out of inertia rather
    # than need; docs/currency-model-decision.md called that out as a known
    # inconsistency and sql/021 (WP2, read-time currency) settled it.
    beginning_balance_cents: int
    ending_balance_cents: int
    # (ending − beginning) − signed sum of the assigned non-deleted
    # transactions. Zero means the batch adds up. Computed at read time from
    # the ledger, never stored — the same rule balances follow (sql/022).
    difference_cents: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def reconciliation_from_row(row) -> dict:
    """Serialize a reconciliation row.

    ``row`` must carry ``difference_cents`` — every reconciliation SELECT goes
    through ``helpers.reconciliations.RECONCILIATION_SELECT``, which projects
    it. A bare ``SELECT *`` raises ``KeyError`` here rather than emitting a
    response with the figure silently missing.
    """
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
        difference_cents=row["difference_cents"],
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
