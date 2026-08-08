"""HTTP handlers for /transactions — thin adapters over helpers.transactions.

The GET endpoints (list + detail) stay here because they're read-only
and have no business logic worth extracting. The mutation endpoints
(POST, PUT, DELETE, POST /batch) delegate to helpers.transactions.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import ERROR_RESPONSES
from app.helpers import transactions as transactions_service
from app.helpers.formatting import apply_debit_as_negative
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import paginated_response
from app.helpers.query_builder import fetch_owned_row_or_404
from app.helpers.validation import extract_update_fields
from app.schemas.pagination import Paginated
from app.schemas.transactions import (
    TransactionBatchRequest,
    TransactionBatchResponse,
    TransactionCreateRequest,
    TransactionResponse,
    TransactionUpdateRequest,
    TransactionWithWarningsResponse,
    transaction_from_row,
)

router = APIRouter(prefix="/transactions", tags=["transactions"], responses=ERROR_RESPONSES)


def _escape_like(term: str) -> str:
    """Make a user-supplied search term literal inside a LIKE/ILIKE pattern."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# GET /transactions/{transaction_id}
# ---------------------------------------------------------------------------
@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: UUID,
    auth_user: CurrentUser,
    debit_as_negative: bool = Query(False),
):
    async with db.pool.acquire() as conn:
        row = await fetch_owned_row_or_404(
            conn, "expense_transactions", transaction_id, auth_user.id, "transaction"
        )
        data = transaction_from_row(row)
        await transactions_service.attach_hashtag_ids(conn, data)
        if debit_as_negative:
            data = apply_debit_as_negative(data)
        return data


# ---------------------------------------------------------------------------
# GET /transactions
# ---------------------------------------------------------------------------
@router.get("", response_model=Paginated[TransactionResponse])
async def list_transactions(
    auth_user: CurrentUser,
    account_id: Optional[UUID] = Query(None),
    category_id: Optional[UUID] = Query(None),
    hashtag_id: Optional[UUID] = Query(None),
    reconciliation_id: Optional[UUID] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    cleared: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    include_deleted: bool = Query(False),
    debit_as_negative: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with db.pool.acquire() as conn:
        conditions = ["t.user_id = $1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("t.deleted_at IS NULL")

        if account_id:
            params.append(account_id)
            conditions.append(f"t.account_id = ${len(params)}")

        if category_id:
            params.append(category_id)
            conditions.append(f"t.category_id = ${len(params)}")

        if hashtag_id:
            params.append(hashtag_id)
            conditions.append(
                f"EXISTS (SELECT 1 FROM expense_transaction_hashtags th "
                f"WHERE th.transaction_id = t.id AND th.hashtag_id = ${len(params)} "
                f"AND th.deleted_at IS NULL)"
            )

        if reconciliation_id:
            params.append(reconciliation_id)
            conditions.append(f"t.reconciliation_id = ${len(params)}")

        if date_from:
            params.append(date_from)
            conditions.append(f"t.date >= ${len(params)}")

        if date_to:
            params.append(date_to)
            conditions.append(f"t.date <= ${len(params)}")

        if cleared is not None:
            params.append(cleared)
            conditions.append(f"t.cleared = ${len(params)}")

        if search:
            pattern = f"%{_escape_like(search)}%"
            params.append(pattern)
            conditions.append(
                f"(t.title ILIKE ${len(params)} ESCAPE '\\' "
                f"OR t.description ILIKE ${len(params)} ESCAPE '\\')"
            )

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_transactions t WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT t.* FROM expense_transactions t
            WHERE {where}
            ORDER BY t.date DESC, t.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [transaction_from_row(row) for row in rows]
        await transactions_service.attach_hashtag_ids(conn, data)
        if debit_as_negative:
            data = [apply_debit_as_negative(d) for d in data]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /transactions
# ---------------------------------------------------------------------------
# The transfer branch returns the primary leg only — same TransactionResponse.
@router.post("", response_model=TransactionResponse, status_code=201)
async def create_transaction(
    body: TransactionCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: transactions_service.create_transaction(
            conn, auth_user.id, body,
        ),
    )


# ---------------------------------------------------------------------------
# PUT /transactions/{transaction_id}
# ---------------------------------------------------------------------------
@router.put("/{transaction_id}", response_model=TransactionResponse)
async def update_transaction(
    transaction_id: UUID,
    body: TransactionUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    # Split out hashtag_ids and reconciliation_id — the helper receives
    # the rest as a mutable ``fields`` dict it can update in place.
    # reconciliation_id accepts null (legitimate "unassign" signal);
    # all other fields reject null via extract_update_fields.
    fields = extract_update_fields(body, nullable={"reconciliation_id"})
    hashtag_ids = fields.pop("hashtag_ids", None)

    recon_id_provided = "reconciliation_id" in fields
    recon_id_value = fields.pop("reconciliation_id", None)

    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: transactions_service.update_transaction(
            conn,
            auth_user.id,
            transaction_id,
            fields,
            hashtag_ids,
            recon_id_provided,
            recon_id_value,
        ),
    )


# ---------------------------------------------------------------------------
# DELETE /transactions/{transaction_id}
# ---------------------------------------------------------------------------
@router.delete("/{transaction_id}", response_model=TransactionWithWarningsResponse)
async def delete_transaction(
    transaction_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: transactions_service.delete_transaction(
            conn, auth_user.id, transaction_id,
        ),
    )


# ---------------------------------------------------------------------------
# POST /transactions/{transaction_id}/restore
# ---------------------------------------------------------------------------
@router.post("/{transaction_id}/restore", response_model=TransactionWithWarningsResponse)
async def restore_transaction(
    transaction_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: transactions_service.restore_transaction(
            conn, auth_user.id, transaction_id,
        ),
    )


# ---------------------------------------------------------------------------
# POST /transactions/batch
# ---------------------------------------------------------------------------
@router.post("/batch", response_model=TransactionBatchResponse, status_code=201)
async def batch_create_transactions(
    body: TransactionBatchRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: transactions_service.create_batch(
            conn, auth_user.id, body,
        ),
    )
