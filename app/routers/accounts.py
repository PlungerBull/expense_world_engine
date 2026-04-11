from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.exchange_rate import get_rate
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.accounts import AccountCreateRequest, AccountResponse, AccountUpdateRequest

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _account_from_row(row, balance_home_cents: Optional[int] = None) -> dict:
    return AccountResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        currency_code=row["currency_code"],
        is_person=row["is_person"],
        color=row["color"],
        current_balance_cents=row["current_balance_cents"],
        current_balance_home_cents=balance_home_cents,
        is_archived=row["is_archived"],
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


async def _get_home_balance(conn, currency_code: str, balance_cents: int, user_id: str) -> Optional[int]:
    """Convert balance to home currency. Returns None if no rate available."""
    settings = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id
    )
    if settings is None:
        return None

    result = await get_rate(
        conn,
        from_currency=currency_code,
        to_currency=settings["main_currency"],
        as_of=datetime.now(timezone.utc).date(),
    )
    if result is None:
        return None

    return round(balance_cents * result[0])


@router.get("")
async def list_accounts(
    auth_user: CurrentUser,
    include_people: bool = Query(False),
    include_archived: bool = Query(False),
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
        if not include_people:
            conditions.append("is_person = false")
        if not include_archived:
            conditions.append("is_archived = false")

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_bank_accounts WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT * FROM expense_bank_accounts
            WHERE {where}
            ORDER BY sort_order ASC, created_at ASC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = []
        for row in rows:
            home = await _get_home_balance(conn, row["currency_code"], row["current_balance_cents"], auth_user.id)
            data.append(_account_from_row(row, home))

        return paginated_response(data, total, limit, offset)


@router.post("", status_code=201)
async def create_account(
    body: AccountCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        # Validate currency_code
        currency = await conn.fetchrow(
            "SELECT code FROM global_currencies WHERE code = $1", body.currency_code
        )
        if currency is None:
            raise validation_error(
                "Invalid currency code.",
                {"currency_code": f"'{body.currency_code}' is not a valid currency code."},
            )

        # Check uniqueness
        existing = await conn.fetchrow(
            """
            SELECT id FROM expense_bank_accounts
            WHERE user_id = $1 AND name = $2 AND currency_code = $3 AND deleted_at IS NULL
            """,
            auth_user.id,
            body.name,
            body.currency_code,
        )
        if existing is not None:
            raise conflict(f"An account named '{body.name}' with currency '{body.currency_code}' already exists.")

        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO expense_bank_accounts
                    (user_id, name, currency_code, color, sort_order, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, now(), now())
                RETURNING *
                """,
                auth_user.id,
                body.name,
                body.currency_code,
                body.color or "#3b82f6",
                body.sort_order or 0,
            )

            home = await _get_home_balance(conn, row["currency_code"], row["current_balance_cents"], auth_user.id)
            response = _account_from_row(row, home)

            await write_activity_log(
                conn, auth_user.id, "account", str(row["id"]), 1,
                after_snapshot=response,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


@router.get("/{account_id}")
async def get_account(account_id: str, auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            account_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("account")

        home = await _get_home_balance(conn, row["currency_code"], row["current_balance_cents"], auth_user.id)
        return _account_from_row(row, home)


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    body: AccountUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    fields = body.model_dump(exclude_none=True)

    # Reject currency_code changes
    if "currency_code" in fields:
        raise validation_error(
            "currency_code is immutable after creation.",
            {"currency_code": "Cannot be changed after account creation."},
        )

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        # Empty update — return current
        if not fields:
            row = await conn.fetchrow(
                "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                account_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("account")
            home = await _get_home_balance(conn, row["currency_code"], row["current_balance_cents"], auth_user.id)
            return _account_from_row(row, home)

        # Check name uniqueness if name is changing
        if "name" in fields:
            existing = await conn.fetchrow(
                """
                SELECT id FROM expense_bank_accounts
                WHERE user_id = $1 AND name = $2 AND id != $3 AND deleted_at IS NULL
                """,
                auth_user.id,
                fields["name"],
                account_id,
            )
            if existing is not None:
                # Need currency to check full uniqueness
                current = await conn.fetchrow(
                    "SELECT currency_code FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                    account_id,
                    auth_user.id,
                )
                if current:
                    dup = await conn.fetchrow(
                        """
                        SELECT id FROM expense_bank_accounts
                        WHERE user_id = $1 AND name = $2 AND currency_code = $3 AND id != $4 AND deleted_at IS NULL
                        """,
                        auth_user.id,
                        fields["name"],
                        current["currency_code"],
                        account_id,
                    )
                    if dup is not None:
                        raise conflict(f"An account named '{fields['name']}' with this currency already exists.")

        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                account_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("account")

            home_before = await _get_home_balance(conn, before_row["currency_code"], before_row["current_balance_cents"], auth_user.id)
            before = _account_from_row(before_row, home_before)

            set_clauses = []
            params = [account_id, auth_user.id]
            for i, (key, value) in enumerate(fields.items(), start=3):
                set_clauses.append(f"{key} = ${i}")
                params.append(value)
            set_clauses.append("updated_at = now()")
            set_clauses.append("version = version + 1")

            query = f"""
                UPDATE expense_bank_accounts
                SET {', '.join(set_clauses)}
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING *
            """
            after_row = await conn.fetchrow(query, *params)
            if after_row is None:
                raise not_found("account")

            home_after = await _get_home_balance(conn, after_row["currency_code"], after_row["current_balance_cents"], auth_user.id)
            after = _account_from_row(after_row, home_after)

            await write_activity_log(
                conn, auth_user.id, "account", account_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        row = await conn.fetchrow(
            "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            account_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("account")

        # Check for active transactions
        has_txns = await conn.fetchval(
            """
            SELECT 1 FROM expense_transactions
            WHERE account_id = $1 AND user_id = $2 AND deleted_at IS NULL
            LIMIT 1
            """,
            account_id,
            auth_user.id,
        )
        if has_txns:
            raise conflict("Account has active transactions. Archive instead.")

        async with conn.transaction():
            home = await _get_home_balance(conn, row["currency_code"], row["current_balance_cents"], auth_user.id)
            before = _account_from_row(row, home)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_bank_accounts
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                account_id,
                auth_user.id,
            )
            home_after = await _get_home_balance(conn, after_row["currency_code"], after_row["current_balance_cents"], auth_user.id)
            after = _account_from_row(after_row, home_after)

            await write_activity_log(
                conn, auth_user.id, "account", account_id, 3,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


@router.post("/{account_id}/archive")
async def archive_account(
    account_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                account_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("account")

            home_before = await _get_home_balance(conn, before_row["currency_code"], before_row["current_balance_cents"], auth_user.id)
            before = _account_from_row(before_row, home_before)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_bank_accounts
                SET is_archived = true, updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                account_id,
                auth_user.id,
            )

            home_after = await _get_home_balance(conn, after_row["currency_code"], after_row["current_balance_cents"], auth_user.id)
            after = _account_from_row(after_row, home_after)

            await write_activity_log(
                conn, auth_user.id, "account", account_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after
