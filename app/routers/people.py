"""HTTP handler for /people — the one route person accounts need.

A person is an `expense_bank_accounts` row with `is_person = true`, not a
separate resource: the balance you record against them *is* the debt. So this
module has exactly one endpoint, and it exists for one reason — `is_person` is
a creation-time-only fact, and `POST /accounts` must never set it (explicit
creation only; a person is never conjured as a side effect of another write).

Everything after creation is account behaviour and lives on `/accounts/{id}`,
which already accepts person rows because none of its lookups filter on
`is_person`: rename/recolor/reorder via `PUT`, plus `DELETE`, `/restore`,
`/archive`, `/unarchive`, and `GET`. Listing is `GET /accounts?include_people=true`
and the `/dashboard` split. A parallel `/people` CRUD namespace would be a
second copy of routes that already work — rejected under CLAUDE.md's "Reuse
before writing" (owner decision, 2026-08-13).
"""

from fastapi import APIRouter

from app.deps import CurrentUser, IdempotencyKey
from app.errors import ERROR_RESPONSES
from app.helpers import accounts as accounts_service
from app.helpers.idempotency import run_idempotent
from app.schemas.accounts import AccountCreateRequest, AccountResponse

router = APIRouter(prefix="/people", tags=["people"], responses=ERROR_RESPONSES)


# The response IS an account — same model, `is_person: true`. A person has no
# transactions when created, so `current_balance_cents` is 0 (and 0 is a
# balance, not a missing value).
@router.post("", response_model=AccountResponse, status_code=201)
async def create_person(
    body: AccountCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: accounts_service.create_account(
            conn, auth_user.id, body.id, body.name, body.currency_code, body.color,
            body.sort_order,
            is_person=True,
        ),
    )
