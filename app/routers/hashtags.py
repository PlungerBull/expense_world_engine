"""HTTP handlers for /hashtags — thin adapters over helpers.hashtags."""

from typing import Optional

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import hashtags as hashtags_service
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import paginated_response
from app.helpers.validation import extract_update_fields
from app.schemas.hashtags import HashtagCreateRequest, HashtagUpdateRequest, hashtag_from_row

router = APIRouter(prefix="/hashtags", tags=["hashtags"])


@router.get("")
async def list_hashtags(
    auth_user: CurrentUser,
    include_deleted: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
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
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: hashtags_service.create_hashtag(
            conn, auth_user.id, body.id, body.name, body.sort_order,
        ),
    )


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
    fields = extract_update_fields(body)
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: hashtags_service.update_hashtag(
            conn, auth_user.id, hashtag_id, fields,
        ),
    )


@router.delete("/{hashtag_id}")
async def delete_hashtag(
    hashtag_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: hashtags_service.delete_hashtag(
            conn, auth_user.id, hashtag_id,
        ),
    )


@router.post("/{hashtag_id}/restore")
async def restore_hashtag(
    hashtag_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: hashtags_service.restore_hashtag(
            conn, auth_user.id, hashtag_id,
        ),
    )
