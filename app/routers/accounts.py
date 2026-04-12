"""HTTP handlers for /accounts — thin adapters over helpers.accounts."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import accounts as accounts_service
from app.helpers.exchange_rate import batch_get_rates
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.accounts import AccountCreateRequest, AccountUpdateRequest, account_from_row

router = APIRouter(prefix="/accounts", tags=["accounts"])


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

        # Batch home-balance conversion. Previously this loop called
        # `_get_home_balance` once per account, which itself fired one query
        # for user_settings and one for the exchange rate — an N+1 pattern
        # that produced ~2N extra DB round-trips per list request. Now:
        #   1. Fetch user_settings ONCE outside the loop.
        #   2. Collect distinct account currencies.
        #   3. Resolve all rates in one deduplicated batch.
        # The loop becomes a pure in-memory transform.
        settings_row = await conn.fetchrow(
            "SELECT main_currency FROM user_settings WHERE user_id = $1",
            auth_user.id,
        )
        main_currency = settings_row["main_currency"] if settings_row else None

        rate_by_currency: dict[str, float] = {}
        if main_currency and rows:
            currencies = {row["currency_code"] for row in rows}
            today = datetime.now(timezone.utc).date()
            rate_by_currency = await batch_get_rates(
                conn, currencies, main_currency, today,
            )

        data = []
        for row in rows:
            rate = rate_by_currency.get(row["currency_code"])
            home = (
                round(row["current_balance_cents"] * rate)
                if rate is not None
                else None
            )
            data.append(account_from_row(row, home))

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

        async with conn.transaction():
            response = await accounts_service.create_account(
                conn, auth_user.id, body.name, body.currency_code, body.color, body.sort_order,
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

        home = await accounts_service.get_home_balance(
            conn, row["currency_code"], row["current_balance_cents"], auth_user.id
        )
        return account_from_row(row, home)


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    body: AccountUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    fields = body.model_dump(exclude_none=True)

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await accounts_service.update_account(
                conn, auth_user.id, account_id, fields,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


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

        async with conn.transaction():
            response = await accounts_service.delete_account(
                conn, auth_user.id, account_id,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


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
            response = await accounts_service.archive_account(
                conn, auth_user.id, account_id,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response
