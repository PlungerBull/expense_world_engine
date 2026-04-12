"""HTTP handlers for /categories — thin adapters over helpers.categories."""

from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import categories as categories_service
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.categories import CategoryCreateRequest, CategoryUpdateRequest, category_from_row

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("")
async def list_categories(
    auth_user: CurrentUser,
    include_deleted: bool = Query(False),
    limit: int = Query(50),
    offset: int = Query(0),
):
    limit = clamp_limit(limit)

    async with db.pool.acquire() as conn:
        conditions = ["user_id = $1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("deleted_at IS NULL")

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_categories WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT * FROM expense_categories
            WHERE {where}
            ORDER BY is_system DESC, sort_order ASC, created_at ASC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [category_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)


@router.post("", status_code=201)
async def create_category(
    body: CategoryCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        async with conn.transaction():
            response = await categories_service.create_category(
                conn, auth_user.id, body.name, body.color, body.sort_order,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


@router.get("/{category_id}")
async def get_category(category_id: str, auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            category_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("category")
        return category_from_row(row)


@router.put("/{category_id}")
async def update_category(
    category_id: str,
    body: CategoryUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    fields = body.model_dump(exclude_none=True)

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await categories_service.update_category(
                conn, auth_user.id, category_id, fields,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


@router.delete("/{category_id}")
async def delete_category(
    category_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await categories_service.delete_category(
                conn, auth_user.id, category_id,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response
