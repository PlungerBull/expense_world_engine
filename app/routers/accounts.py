"""HTTP handlers for /accounts — thin adapters over helpers.accounts."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import ERROR_RESPONSES
from app.helpers import accounts as accounts_service
from app.helpers.account_balance import fetch_balance, fetch_balances
from app.helpers.exchange_rate import batch_get_rates, rate_lookup_date
from app.helpers.idempotency import run_idempotent
from app.helpers.pagination import paginated_response
from app.helpers.query_builder import fetch_owned_row_or_404
from app.helpers.validation import extract_update_fields
from app.schemas.accounts import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    OpeningBalanceRequest,
    account_from_row,
)
from app.schemas.pagination import Paginated
from app.schemas.transactions import TransactionResponse

router = APIRouter(prefix="/accounts", tags=["accounts"], responses=ERROR_RESPONSES)


@router.get("", response_model=Paginated[AccountResponse])
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

        # Batch home-balance conversion. Previously each account in this loop
        # paid its own settings read + rate lookup (the per-account path that
        # survives as helpers/accounts.get_home_balance) — an N+1 pattern
        # that produced ~2N extra DB round-trips per list request. Now:
        #   1. Fetch user_settings ONCE outside the loop.
        #   2. Collect distinct account currencies.
        #   3. Resolve all rates in one deduplicated batch.
        # The loop becomes a pure in-memory transform.
        settings_row = await conn.fetchrow(
            "SELECT main_currency, display_timezone FROM user_settings WHERE user_id = $1",
            auth_user.id,
        )
        main_currency = settings_row["main_currency"] if settings_row else None

        rate_by_currency: dict[str, float] = {}
        if main_currency and rows:
            currencies = {row["currency_code"] for row in rows}
            today = rate_lookup_date(settings_row["display_timezone"])
            rate_by_currency = await batch_get_rates(
                conn, currencies, main_currency, today,
            )

        # Balances for exactly the accounts on this page, in one query. Scoped to
        # the page rather than the whole ledger so the index on
        # (user_id, account_id) can drive it; `fetch_balances` seeds every
        # requested id with 0, so an account with no transactions is 0 rather
        # than a missing key.
        balances = await fetch_balances(conn, auth_user.id, [r["id"] for r in rows])

        data = []
        for row in rows:
            balance_cents = balances[str(row["id"])]
            rate = rate_by_currency.get(row["currency_code"])
            home = round(balance_cents * rate) if rate is not None else None
            data.append(account_from_row(row, balance_cents, home))

        return paginated_response(data, total, limit, offset)


@router.post("", response_model=AccountResponse, status_code=201)
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


# An opening balance IS a transaction — the response is the ledger row it seeds.
@router.post("/{account_id}/opening-balance", response_model=TransactionResponse, status_code=201)
async def create_opening_balance(
    account_id: UUID,
    body: OpeningBalanceRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: accounts_service.create_opening_balance(
            conn, auth_user.id, account_id, body,
        ),
    )


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: UUID, auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        row = await fetch_owned_row_or_404(
            conn, "expense_bank_accounts", account_id, auth_user.id, "account"
        )

        balance_cents = await fetch_balance(conn, auth_user.id, account_id)
        home = await accounts_service.get_home_balance(
            conn, row["currency_code"], balance_cents, auth_user.id
        )
        return account_from_row(row, balance_cents, home)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: UUID,
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


@router.delete("/{account_id}", response_model=AccountResponse)
async def delete_account(
    account_id: UUID,
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


@router.post("/{account_id}/restore", response_model=AccountResponse)
async def restore_account(
    account_id: UUID,
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


@router.post("/{account_id}/archive", response_model=AccountResponse)
async def archive_account(
    account_id: UUID,
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


@router.post("/{account_id}/unarchive", response_model=AccountResponse)
async def unarchive_account(
    account_id: UUID,
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
