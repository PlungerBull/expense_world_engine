"""HTTP handlers for /accounts — thin adapters over helpers.accounts."""

from uuid import UUID

from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser, IdempotencyKey, Limit, Offset
from app.errors import ERROR_RESPONSES
from app.helpers import accounts as accounts_service
from app.helpers.account_balance import (
    fetch_balance,
    fetch_balances,
    fetch_home_balance,
    fetch_home_balances,
)
from app.helpers.exchange_rate import rate_lookup_date
from app.helpers.idempotency import run_idempotent
from app.helpers.settings import get_user_report_settings
from app.helpers.pagination import DEFAULT_LIMIT, list_page, paginated_response
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
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
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

        rows, total = await list_page(
            conn,
            from_sql="expense_bank_accounts",
            conditions=conditions,
            params=params,
            order_by="sort_order ASC, created_at ASC",
            limit=limit,
            offset=offset,
        )

        # One settings read (422 SETTINGS_MISSING, like every home-converting
        # surface — owner decision 2026-08-08), one clock read, then balances
        # for exactly the accounts on this page in one query (`fetch_balances`
        # seeds every requested id with 0) and one batched conversion.
        settings = await get_user_report_settings(conn, auth_user.id)
        today = rate_lookup_date(settings["display_timezone"])
        balances = await fetch_balances(conn, auth_user.id, [r["id"] for r in rows])
        homes = await fetch_home_balances(
            conn,
            main_currency=settings["main_currency"],
            today=today,
            balances=balances,
            currency_by_id={str(r["id"]): r["currency_code"] for r in rows},
        )

        data = [
            account_from_row(row, balances[str(row["id"])], homes[str(row["id"])])
            for row in rows
        ]
        return paginated_response(data, total, limit, offset)


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
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
    x_idempotency_key: IdempotencyKey = None,
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
        home = await fetch_home_balance(
            conn, auth_user.id, row["currency_code"], balance_cents
        )
        return account_from_row(row, balance_cents, home)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: UUID,
    body: AccountUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
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
    x_idempotency_key: IdempotencyKey = None,
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
    x_idempotency_key: IdempotencyKey = None,
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
    x_idempotency_key: IdempotencyKey = None,
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
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: accounts_service.unarchive_account(
            conn, auth_user.id, account_id,
        ),
    )
