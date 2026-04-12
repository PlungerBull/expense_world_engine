"""HTTP handlers for /inbox — thin adapters over helpers.inbox.

GET endpoints (list + detail) stay here. Mutation endpoints (POST, PUT,
DELETE, POST /promote) delegate to helpers.inbox.
"""

from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import inbox as inbox_service
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.inbox import InboxCreateRequest, InboxUpdateRequest, inbox_from_row

router = APIRouter(prefix="/inbox", tags=["inbox"])


# ---------------------------------------------------------------------------
# GET /inbox
# ---------------------------------------------------------------------------
@router.get("")
async def list_inbox(
    auth_user: CurrentUser,
    ready: bool = Query(False),
    overdue: bool = Query(False),
    include_deleted: bool = Query(False),
    limit: int = Query(50),
    offset: int = Query(0),
):
    limit = clamp_limit(limit)

    async with db.pool.acquire() as conn:
        conditions = ["i.user_id = $1", "i.status = 1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("i.deleted_at IS NULL")

        if ready:
            conditions.append("i.title IS NOT NULL")
            conditions.append("i.title != 'UNTITLED'")
            conditions.append("i.amount_cents IS NOT NULL")
            conditions.append("i.amount_cents != 0")
            conditions.append("i.date IS NOT NULL")
            conditions.append("i.date <= now()")
            conditions.append("i.account_id IS NOT NULL")
            conditions.append("i.category_id IS NOT NULL")
            # Account must be active and non-archived
            conditions.append(
                "EXISTS (SELECT 1 FROM expense_bank_accounts a "
                "WHERE a.id = i.account_id AND a.deleted_at IS NULL AND a.is_archived = false)"
            )
            # Category must be active
            conditions.append(
                "EXISTS (SELECT 1 FROM expense_categories c "
                "WHERE c.id = i.category_id AND c.deleted_at IS NULL)"
            )

        if overdue:
            conditions.append("i.date IS NOT NULL")
            conditions.append("i.date < now()")

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_transaction_inbox i WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT i.* FROM expense_transaction_inbox i
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [inbox_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /inbox
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
async def create_inbox_item(
    body: InboxCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        async with conn.transaction():
            response = await inbox_service.create_inbox_item(
                conn, auth_user.id, body,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


# ---------------------------------------------------------------------------
# GET /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.get("/{inbox_id}")
async def get_inbox_item(inbox_id: str, auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            inbox_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("inbox item")
        return inbox_from_row(row)


# ---------------------------------------------------------------------------
# PUT /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.put("/{inbox_id}")
async def update_inbox_item(
    inbox_id: str,
    body: InboxUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await inbox_service.update_inbox_item(
                conn, auth_user.id, inbox_id, body,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


# ---------------------------------------------------------------------------
# DELETE /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.delete("/{inbox_id}")
async def delete_inbox_item(
    inbox_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await inbox_service.delete_inbox_item(
                conn, auth_user.id, inbox_id,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


# ---------------------------------------------------------------------------
# POST /inbox/{inbox_id}/promote
# ---------------------------------------------------------------------------
@router.post("/{inbox_id}/promote")
async def promote_inbox_item(
    inbox_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await inbox_service.promote_inbox_item(
                conn, auth_user.id, inbox_id,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response
