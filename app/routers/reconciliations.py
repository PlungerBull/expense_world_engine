"""HTTP handlers for /reconciliations — thin adapters over helpers.reconciliations."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser, DebitAsNegative, IdempotencyKey, Limit, Offset
from app.errors import ERROR_RESPONSES, not_found
from app.helpers import reconciliations as reconciliations_service
from app.helpers.formatting import apply_debit_as_negative
from app.helpers.hashtag_links import attach_hashtag_ids
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import DEFAULT_LIMIT, list_page, paginated_response
from app.helpers.validation import extract_update_fields
from app.schemas.pagination import Paginated
from app.schemas.reconciliations import (
    ReconciliationCreateRequest,
    ReconciliationDetailResponse,
    ReconciliationResponse,
    ReconciliationUpdateRequest,
    reconciliation_from_row,
)
from app.schemas.transactions import transaction_from_row

router = APIRouter(
    prefix="/reconciliations", tags=["reconciliations"], responses=ERROR_RESPONSES
)


# ---------------------------------------------------------------------------
# GET /reconciliations
# ---------------------------------------------------------------------------
@router.get("", response_model=Paginated[ReconciliationResponse])
async def list_reconciliations(
    auth_user: CurrentUser,
    account_id: Optional[UUID] = Query(None),
    include_deleted: bool = Query(False),
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
):
    async with db.pool.acquire() as conn:
        conditions = ["rec.user_id = $1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("rec.deleted_at IS NULL")
        if account_id is not None:
            params.append(account_id)
            conditions.append(f"rec.account_id = ${len(params)}")

        # Deliberate `list_page` non-adopter (bloat-audit §12): the page SELECT
        # is RECONCILIATION_SELECT, which carries its own FROM plus the
        # difference_cents correlated subquery, while the count deliberately
        # uses this cheaper FROM that skips it. The helper's independent
        # from_sql/select knobs can't express that split.
        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_reconciliations rec WHERE {where}",
            *params,
        )

        # Per-account lists are chronological: a reconciliation is a
        # statement period, so its start date is its natural position.
        # Rows with no date (both dates are nullable, and the PUT allows
        # clearing them) sort last, newest-created first among themselves.
        # Cross-account lists fall back to created_at DESC — period dates
        # across accounts are unrelated statements.
        order_clause = (
            "ORDER BY rec.date_start ASC NULLS LAST, rec.created_at ASC"
            if account_id is not None
            else "ORDER BY rec.created_at DESC"
        )

        rows = await conn.fetch(
            f"""
            {reconciliations_service.RECONCILIATION_SELECT}
            WHERE {where}
            {order_clause}
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [reconciliation_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /reconciliations
# ---------------------------------------------------------------------------
@router.post("", response_model=ReconciliationResponse, status_code=201)
async def create_reconciliation(
    body: ReconciliationCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: reconciliations_service.create_reconciliation(
            conn,
            auth_user.id,
            body.id,
            body.account_id,
            body.name,
            body.date_start,
            body.date_end,
            body.beginning_balance_cents,
            body.ending_balance_cents,
        ),
    )


# ---------------------------------------------------------------------------
# GET /reconciliations/{reconciliation_id}
# ---------------------------------------------------------------------------
@router.get("/{reconciliation_id}", response_model=ReconciliationDetailResponse)
async def get_reconciliation(
    reconciliation_id: UUID,
    auth_user: CurrentUser,
    debit_as_negative: DebitAsNegative = False,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
):
    async with db.pool.acquire() as conn:
        row = await reconciliations_service.fetch_reconciliation(
            conn, auth_user.id, reconciliation_id,
        )
        if row is None:
            raise not_found("reconciliation")

        txn_rows, transactions_total = await list_page(
            conn,
            from_sql="expense_transactions",
            conditions=[
                "reconciliation_id = $1",
                "user_id = $2",
                "deleted_at IS NULL",
            ],
            params=[reconciliation_id, auth_user.id],
            order_by="date DESC, created_at DESC",
            limit=limit,
            offset=offset,
        )

        recon = reconciliation_from_row(row)
        txns = [transaction_from_row(r) for r in txn_rows]
        await attach_hashtag_ids(conn, txns)
        if debit_as_negative:
            txns = [apply_debit_as_negative(t) for t in txns]

        # ``transactions_truncated`` stays in the response so existing clients
        # don't need immediate updates; it now means "there are more rows
        # beyond this page", derived from the total vs. the current window.
        transactions_truncated = (offset + len(txn_rows)) < transactions_total

        return ReconciliationDetailResponse.model_validate(
            {
                **recon,
                "transactions": txns,
                "transactions_total": transactions_total,
                "transactions_limit": limit,
                "transactions_offset": offset,
                "transactions_truncated": transactions_truncated,
            }
        ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# PUT /reconciliations/{reconciliation_id}
# ---------------------------------------------------------------------------
@router.put("/{reconciliation_id}", response_model=ReconciliationResponse)
async def update_reconciliation(
    reconciliation_id: UUID,
    body: ReconciliationUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    # date_start / date_end are legitimately nullable (user can clear a date
    # to "reopen" the range). All other fields reject null.
    fields = extract_update_fields(body, nullable={"date_start", "date_end"})
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.update_reconciliation(
            conn, auth_user.id, reconciliation_id, fields,
        ),
    )


# ---------------------------------------------------------------------------
# POST /reconciliations/{reconciliation_id}/complete
# ---------------------------------------------------------------------------
@router.post("/{reconciliation_id}/complete", response_model=ReconciliationResponse)
async def complete_reconciliation(
    reconciliation_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.complete_reconciliation(
            conn, auth_user.id, reconciliation_id,
        ),
    )


# ---------------------------------------------------------------------------
# POST /reconciliations/{reconciliation_id}/revert
# ---------------------------------------------------------------------------
@router.post("/{reconciliation_id}/revert", response_model=ReconciliationResponse)
async def revert_reconciliation(
    reconciliation_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.revert_reconciliation(
            conn, auth_user.id, reconciliation_id,
        ),
    )


# ---------------------------------------------------------------------------
# DELETE /reconciliations/{reconciliation_id}
# ---------------------------------------------------------------------------
@router.delete("/{reconciliation_id}", response_model=ReconciliationResponse)
async def delete_reconciliation(
    reconciliation_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.delete_reconciliation(
            conn, auth_user.id, reconciliation_id,
        ),
    )


# ---------------------------------------------------------------------------
# POST /reconciliations/{reconciliation_id}/restore
# ---------------------------------------------------------------------------
@router.post("/{reconciliation_id}/restore", response_model=ReconciliationResponse)
async def restore_reconciliation(
    reconciliation_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.restore_reconciliation(
            conn, auth_user.id, reconciliation_id,
        ),
    )
