"""HTTP handlers for /reconciliations — thin adapters over helpers.reconciliations."""

from typing import Optional

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import reconciliations as reconciliations_service
from app.helpers.formatting import apply_debit_as_negative
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import paginated_response
from app.helpers.validation import extract_update_fields
from app.schemas.reconciliations import (
    ReconciliationCreateRequest,
    ReconciliationDetailResponse,
    ReconciliationUpdateRequest,
    reconciliation_from_row,
)
from app.schemas.transactions import transaction_from_row

router = APIRouter(prefix="/reconciliations", tags=["reconciliations"])


# ---------------------------------------------------------------------------
# GET /reconciliations
# ---------------------------------------------------------------------------
@router.get("")
async def list_reconciliations(
    auth_user: CurrentUser,
    account_id: Optional[str] = Query(None),
    include_deleted: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with db.pool.acquire() as conn:
        conditions = ["user_id = $1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        if account_id is not None:
            params.append(account_id)
            conditions.append(f"account_id = ${len(params)}")

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_reconciliations WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT * FROM expense_reconciliations
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        rate_by_id = await reconciliations_service.resolve_home_rates(
            conn, auth_user.id, list(rows)
        )
        data = [reconciliation_from_row(row, rate_by_id.get(str(row["id"]))) for row in rows]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /reconciliations
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
async def create_reconciliation(
    body: ReconciliationCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
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
@router.get("/{reconciliation_id}")
async def get_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    debit_as_negative: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            reconciliation_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("reconciliation")

        transactions_total = await conn.fetchval(
            """
            SELECT count(*) FROM expense_transactions
            WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
            """,
            reconciliation_id,
            auth_user.id,
        )

        txn_rows = await conn.fetch(
            """
            SELECT * FROM expense_transactions
            WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
            ORDER BY date DESC, created_at DESC
            LIMIT $3 OFFSET $4
            """,
            reconciliation_id,
            auth_user.id,
            limit,
            offset,
        )

        rate_by_id = await reconciliations_service.resolve_home_rates(
            conn, auth_user.id, [row]
        )
        recon = reconciliation_from_row(row, rate_by_id.get(str(row["id"])))
        txns = [transaction_from_row(r) for r in txn_rows]
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
@router.put("/{reconciliation_id}")
async def update_reconciliation(
    reconciliation_id: str,
    body: ReconciliationUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
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
@router.post("/{reconciliation_id}/complete")
async def complete_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
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
@router.post("/{reconciliation_id}/revert")
async def revert_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
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
@router.delete("/{reconciliation_id}")
async def delete_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
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
@router.post("/{reconciliation_id}/restore")
async def restore_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.restore_reconciliation(
            conn, auth_user.id, reconciliation_id,
        ),
    )
