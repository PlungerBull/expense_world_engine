"""HTTP handlers for /hashtags — thin adapters over helpers.hashtags."""

from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import hashtags as hashtags_service
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.hashtags import HashtagCreateRequest, HashtagUpdateRequest, hashtag_from_row

router = APIRouter(prefix="/hashtags", tags=["hashtags"])


@router.get("")
async def list_hashtags(
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
            f"SELECT count(*) FROM expense_hashtags WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT * FROM expense_hashtags
            WHERE {where}
            ORDER BY sort_order ASC, created_at ASC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [hashtag_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)


@router.post("", status_code=201)
async def create_hashtag(
    body: HashtagCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        async with conn.transaction():
            response = await hashtags_service.create_hashtag(
                conn, auth_user.id, body.name, body.sort_order,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


@router.get("/{hashtag_id}")
async def get_hashtag(hashtag_id: str, auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            hashtag_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("hashtag")
        return hashtag_from_row(row)


@router.put("/{hashtag_id}")
async def update_hashtag(
    hashtag_id: str,
    body: HashtagUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    fields = body.model_dump(exclude_none=True)

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await hashtags_service.update_hashtag(
                conn, auth_user.id, hashtag_id, fields,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


@router.delete("/{hashtag_id}")
async def delete_hashtag(
    hashtag_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await hashtags_service.delete_hashtag(
                conn, auth_user.id, hashtag_id,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response
