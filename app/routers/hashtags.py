from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import conflict, not_found
from app.helpers.activity_log import write_activity_log
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.hashtags import HashtagCreateRequest, HashtagResponse, HashtagUpdateRequest

router = APIRouter(prefix="/hashtags", tags=["hashtags"])


def _hashtag_from_row(row) -> dict:
    return HashtagResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


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

        data = [_hashtag_from_row(row) for row in rows]
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

        # Check uniqueness
        existing = await conn.fetchrow(
            """
            SELECT id FROM expense_hashtags
            WHERE user_id = $1 AND name = $2 AND deleted_at IS NULL
            """,
            auth_user.id,
            body.name,
        )
        if existing is not None:
            raise conflict(f"A hashtag named '{body.name}' already exists.")

        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO expense_hashtags
                    (user_id, name, sort_order, created_at, updated_at)
                VALUES ($1, $2, $3, now(), now())
                RETURNING *
                """,
                auth_user.id,
                body.name,
                body.sort_order or 0,
            )

            response = _hashtag_from_row(row)

            await write_activity_log(
                conn, auth_user.id, "hashtag", str(row["id"]), 1,
                after_snapshot=response,
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
        return _hashtag_from_row(row)


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

        # Empty update — return current
        if not fields:
            row = await conn.fetchrow(
                "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                hashtag_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("hashtag")
            return _hashtag_from_row(row)

        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                hashtag_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("hashtag")

            before = _hashtag_from_row(before_row)

            # Check name uniqueness if changing
            if "name" in fields:
                dup = await conn.fetchrow(
                    """
                    SELECT id FROM expense_hashtags
                    WHERE user_id = $1 AND name = $2 AND id != $3 AND deleted_at IS NULL
                    """,
                    auth_user.id,
                    fields["name"],
                    hashtag_id,
                )
                if dup is not None:
                    raise conflict(f"A hashtag named '{fields['name']}' already exists.")

            set_clauses = []
            params = [hashtag_id, auth_user.id]
            for i, (key, value) in enumerate(fields.items(), start=3):
                set_clauses.append(f"{key} = ${i}")
                params.append(value)
            set_clauses.append("updated_at = now()")
            set_clauses.append("version = version + 1")

            query = f"""
                UPDATE expense_hashtags
                SET {', '.join(set_clauses)}
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING *
            """
            after_row = await conn.fetchrow(query, *params)
            if after_row is None:
                raise not_found("hashtag")

            after = _hashtag_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "hashtag", hashtag_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


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

        row = await conn.fetchrow(
            "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            hashtag_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("hashtag")

        async with conn.transaction():
            before = _hashtag_from_row(row)

            # Soft-delete all junction rows for this hashtag
            await conn.execute(
                """
                UPDATE expense_transaction_hashtags
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE hashtag_id = $1 AND user_id = $2 AND deleted_at IS NULL
                """,
                hashtag_id,
                auth_user.id,
            )

            after_row = await conn.fetchrow(
                """
                UPDATE expense_hashtags
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                hashtag_id,
                auth_user.id,
            )
            after = _hashtag_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "hashtag", hashtag_id, 3,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after
