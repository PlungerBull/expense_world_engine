"""HTTP handlers for /accounts — thin adapters over helpers.accounts."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import accounts as accounts_service
from app.helpers import reconciliations as reconciliations_service
from app.helpers.exchange_rate import batch_get_rates
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import paginated_response
from app.helpers.validation import extract_update_fields
from app.schemas.accounts import AccountCreateRequest, AccountUpdateRequest, account_from_row
from app.schemas.reconciliations import ReconciliationReorderRequest

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("")
async def list_accounts(
    auth_user: CurrentUser,
    include_people: bool = Query(False),
    include_archived: bool = Query(False),
    include_deleted: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
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
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: accounts_service.create_account(
            conn, auth_user.id, body.id, body.name, body.currency_code, body.color, body.sort_order,
        ),
    )


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
    fields = extract_update_fields(body)
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: accounts_service.update_account(
            conn, auth_user.id, account_id, fields,
        ),
    )


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: accounts_service.delete_account(
            conn, auth_user.id, account_id,
        ),
    )


@router.post("/{account_id}/restore")
async def restore_account(
    account_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: accounts_service.restore_account(
            conn, auth_user.id, account_id,
        ),
    )


@router.post("/{account_id}/archive")
async def archive_account(
    account_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: accounts_service.archive_account(
            conn, auth_user.id, account_id,
        ),
    )


@router.post("/{account_id}/unarchive")
async def unarchive_account(
    account_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: accounts_service.unarchive_account(
            conn, auth_user.id, account_id,
        ),
    )


# ---------------------------------------------------------------------------
# PUT /accounts/{account_id}/reconciliations/order
#
# Bulk-reorder this account's reconciliations and run the chained-balance
# cascade in one DB transaction. Lives here (not on /reconciliations) so
# the URL reflects the parent-scope semantics: an order is meaningful
# only within one account.
# ---------------------------------------------------------------------------
@router.put("/{account_id}/reconciliations/order")
async def reorder_account_reconciliations(
    account_id: str,
    body: ReconciliationReorderRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: reconciliations_service.reorder_reconciliations(
            conn, auth_user.id, account_id, body.ordered_ids,
        ),
    )
