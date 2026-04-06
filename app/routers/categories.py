from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import conflict, forbidden, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.categories import CategoryCreateRequest, CategoryResponse, CategoryUpdateRequest

router = APIRouter(prefix="/categories", tags=["categories"])


def _category_from_row(row) -> dict:
    return CategoryResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        color=row["color"],
        is_system=row["is_system"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


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

        data = [_category_from_row(row) for row in rows]
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

        # Check uniqueness
        existing = await conn.fetchrow(
            """
            SELECT id FROM expense_categories
            WHERE user_id = $1 AND name = $2 AND deleted_at IS NULL
            """,
            auth_user.id,
            body.name,
        )
        if existing is not None:
            raise conflict(f"A category named '{body.name}' already exists.")

        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO expense_categories
                    (user_id, name, color, sort_order, created_at, updated_at)
                VALUES ($1, $2, $3, $4, now(), now())
                RETURNING *
                """,
                auth_user.id,
                body.name,
                body.color,
                body.sort_order or 0,
            )

            response = _category_from_row(row)

            await write_activity_log(
                conn, auth_user.id, "category", str(row["id"]), 1,
                after_snapshot=response,
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
        return _category_from_row(row)


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

        # Empty update — return current
        if not fields:
            row = await conn.fetchrow(
                "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                category_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("category")
            return _category_from_row(row)

        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                category_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("category")

            # System categories cannot be renamed
            if before_row["is_system"] and "name" in fields:
                raise forbidden(f"Cannot rename system category {before_row['name']}.")

            before = _category_from_row(before_row)

            # Check name uniqueness if changing
            if "name" in fields:
                dup = await conn.fetchrow(
                    """
                    SELECT id FROM expense_categories
                    WHERE user_id = $1 AND name = $2 AND id != $3 AND deleted_at IS NULL
                    """,
                    auth_user.id,
                    fields["name"],
                    category_id,
                )
                if dup is not None:
                    raise conflict(f"A category named '{fields['name']}' already exists.")

            set_clauses = []
            params = [category_id, auth_user.id]
            for i, (key, value) in enumerate(fields.items(), start=3):
                set_clauses.append(f"{key} = ${i}")
                params.append(value)
            set_clauses.append("updated_at = now()")
            set_clauses.append("version = version + 1")

            query = f"""
                UPDATE expense_categories
                SET {', '.join(set_clauses)}
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING *
            """
            after_row = await conn.fetchrow(query, *params)
            if after_row is None:
                raise not_found("category")

            after = _category_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "category", category_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


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

        row = await conn.fetchrow(
            "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            category_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("category")

        # System categories cannot be deleted
        if row["is_system"]:
            raise forbidden(f"Cannot delete system category {row['name']}.")

        # Check for referencing transactions (ledger + inbox)
        has_txns = await conn.fetchval(
            """
            SELECT 1 FROM expense_transactions
            WHERE category_id = $1 AND user_id = $2 AND deleted_at IS NULL
            LIMIT 1
            """,
            category_id,
            auth_user.id,
        )
        if has_txns:
            raise conflict("Category is referenced by active transactions. Remove those references first.")

        has_inbox = await conn.fetchval(
            """
            SELECT 1 FROM expense_transaction_inbox
            WHERE category_id = $1 AND user_id = $2 AND deleted_at IS NULL
            LIMIT 1
            """,
            category_id,
            auth_user.id,
        )
        if has_inbox:
            raise conflict("Category is referenced by active inbox items. Remove those references first.")

        async with conn.transaction():
            before = _category_from_row(row)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_categories
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                category_id,
                auth_user.id,
            )
            after = _category_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "category", category_id, 3,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after
