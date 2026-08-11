"""HTTP handlers for /inbox — thin adapters over helpers.inbox.

GET endpoints (list + detail) stay here. Mutation endpoints (POST, PUT,
DELETE, POST /promote) delegate to helpers.inbox.
"""

from uuid import UUID

from fastapi import APIRouter, Query

from app import db
from app.constants import InboxStatus
from app.deps import CurrentUser, DebitAsNegative, IdempotencyKey, Limit, Offset
from app.errors import ERROR_RESPONSES
from app.helpers import inbox as inbox_service
from app.helpers.formatting import apply_debit_as_negative
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import DEFAULT_LIMIT, list_page, paginated_response
from app.helpers.query_builder import fetch_owned_row_or_404
from app.schemas.inbox import (
    InboxCreateRequest,
    InboxPromoteRequest,
    InboxResponse,
    InboxUpdateRequest,
    inbox_from_row,
)
from app.schemas.pagination import Paginated
from app.schemas.transactions import TransactionResponse

router = APIRouter(prefix="/inbox", tags=["inbox"], responses=ERROR_RESPONSES)


# ---------------------------------------------------------------------------
# GET /inbox
# ---------------------------------------------------------------------------
@router.get("", response_model=Paginated[InboxResponse])
async def list_inbox(
    auth_user: CurrentUser,
    ready: bool = Query(False),
    overdue: bool = Query(False),
    include_deleted: bool = Query(False),
    debit_as_negative: DebitAsNegative = False,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
):
    async with db.pool.acquire() as conn:
        conditions = ["i.user_id = $1", "i.status = $2"]
        params: list = [auth_user.id, int(InboxStatus.PENDING)]

        if not include_deleted:
            conditions.append("i.deleted_at IS NULL")

        if ready:
            # This predicate must agree with promote_inbox_item's validation
            # block (helpers/inbox.py) in both directions: everything listed
            # here must promote, and everything that promotes must be listed.
            # The two are written separately — SQL here, Python there — which is
            # exactly how they drifted (WP7.2/7.3).
            conditions.append("i.title IS NOT NULL")
            conditions.append("i.title != 'UNTITLED'")
            conditions.append("i.amount_cents IS NOT NULL")
            conditions.append("i.amount_cents != 0")
            conditions.append("i.date IS NOT NULL")
            conditions.append("i.date <= now()")
            conditions.append("i.account_id IS NOT NULL")
            # Account must be active and non-archived. The a.user_id arm
            # matches the Python side's tenant scope (CLAUDE.md: engine-side
            # user_id predicates are the only isolation) — without it a draft
            # referencing another tenant's account showed as ready while
            # promote 422ed.
            conditions.append(
                "EXISTS (SELECT 1 FROM expense_bank_accounts a "
                "WHERE a.id = i.account_id AND a.user_id = i.user_id "
                "AND a.deleted_at IS NULL AND a.is_archived = false)"
            )
            # Category must be present and active (an inbox row pointing at a
            # deleted category isn't promotable — promote would 422 on the
            # same guard, so excluding it from `?ready=true` keeps the
            # client-facing list honest).
            conditions.append(
                "(i.category_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM expense_categories c "
                "WHERE c.id = i.category_id AND c.user_id = i.user_id "
                "AND c.deleted_at IS NULL))"
            )

        if overdue:
            conditions.append("i.date IS NOT NULL")
            conditions.append("i.date < now()")

        rows, total = await list_page(
            conn,
            from_sql="expense_transaction_inbox i",
            conditions=conditions,
            params=params,
            order_by="i.created_at DESC",
            limit=limit,
            offset=offset,
            select="i.*",
        )

        data = [inbox_from_row(row) for row in rows]
        if debit_as_negative:
            data = [apply_debit_as_negative(d) for d in data]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /inbox
# ---------------------------------------------------------------------------
@router.post("", response_model=InboxResponse, status_code=201)
async def create_inbox_item(
    body: InboxCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: inbox_service.create_inbox_item(
            conn, auth_user.id, body,
        ),
    )


# ---------------------------------------------------------------------------
# GET /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.get("/{inbox_id}", response_model=InboxResponse)
async def get_inbox_item(
    inbox_id: UUID,
    auth_user: CurrentUser,
    debit_as_negative: DebitAsNegative = False,
):
    async with db.pool.acquire() as conn:
        row = await fetch_owned_row_or_404(
            conn, "expense_transaction_inbox", inbox_id, auth_user.id, "inbox item"
        )
        data = inbox_from_row(row)
        if debit_as_negative:
            data = apply_debit_as_negative(data)
        return data


# ---------------------------------------------------------------------------
# PUT /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.put("/{inbox_id}", response_model=InboxResponse)
async def update_inbox_item(
    inbox_id: UUID,
    body: InboxUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: inbox_service.update_inbox_item(
            conn, auth_user.id, inbox_id, body,
        ),
    )


# ---------------------------------------------------------------------------
# DELETE /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.delete("/{inbox_id}", response_model=InboxResponse)
async def delete_inbox_item(
    inbox_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: inbox_service.delete_inbox_item(
            conn, auth_user.id, inbox_id,
        ),
    )


# ---------------------------------------------------------------------------
# POST /inbox/{inbox_id}/restore
# ---------------------------------------------------------------------------
@router.post("/{inbox_id}/restore", response_model=InboxResponse)
async def restore_inbox_item(
    inbox_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: inbox_service.restore_inbox_item(
            conn, auth_user.id, inbox_id,
        ),
    )


# ---------------------------------------------------------------------------
# POST /inbox/{inbox_id}/promote
# ---------------------------------------------------------------------------
# Promote returns the ledger row the draft became.
@router.post("/{inbox_id}/promote", response_model=TransactionResponse)
async def promote_inbox_item(
    inbox_id: UUID,
    body: InboxPromoteRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: inbox_service.promote_inbox_item(
            conn, auth_user.id, inbox_id, body.id,
        ),
    )
