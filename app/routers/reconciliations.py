from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.reconciliations import (
    ReconciliationCreateRequest,
    ReconciliationUpdateRequest,
    reconciliation_from_row,
)
from app.schemas.transactions import transaction_from_row

router = APIRouter(prefix="/reconciliations", tags=["reconciliations"])


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _apply_debit_as_negative(data: dict) -> dict:
    """Post-process a transaction dict to negate amounts for expenses/debits."""
    t = data["transaction_type"]
    d = data.get("transfer_direction")
    if t == 1 or (t == 3 and d == 1):
        data = {**data}
        data["amount_cents"] = -data["amount_cents"]
        if data["amount_home_cents"] is not None:
            data["amount_home_cents"] = -data["amount_home_cents"]
    return data


# ---------------------------------------------------------------------------
# GET /reconciliations
# ---------------------------------------------------------------------------
@router.get("")
async def list_reconciliations(
    auth_user: CurrentUser,
    account_id: Optional[str] = Query(None),
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
        if account_id is not None:
            params.append(account_id)
            conditions.append(f"account_id = ${len(params)}")

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_reconciliations WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT * FROM expense_reconciliations
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [reconciliation_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /reconciliations
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
async def create_reconciliation(
    body: ReconciliationCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        # Validate account_id
        account = await conn.fetchrow(
            """
            SELECT id FROM expense_bank_accounts
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
            """,
            body.account_id,
            auth_user.id,
        )
        if account is None:
            raise validation_error(
                "Account validation failed.",
                {"account_id": "Must reference an active, non-archived account."},
            )

        # Validate name
        if not body.name or not body.name.strip():
            raise validation_error(
                "Name must not be empty.",
                {"name": "Must not be empty."},
            )

        # Auto-prefill beginning_balance_cents from previous batch
        beginning = body.beginning_balance_cents
        if beginning is None:
            prev = await conn.fetchrow(
                """
                SELECT ending_balance_cents FROM expense_reconciliations
                WHERE account_id = $1 AND user_id = $2 AND deleted_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                body.account_id,
                auth_user.id,
            )
            beginning = prev["ending_balance_cents"] if prev else 0

        ending = body.ending_balance_cents if body.ending_balance_cents is not None else 0

        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO expense_reconciliations
                    (user_id, account_id, name, date_start, date_end, status,
                     beginning_balance_cents, ending_balance_cents, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, 1, $6, $7, now(), now())
                RETURNING *
                """,
                auth_user.id,
                body.account_id,
                body.name.strip(),
                body.date_start,
                body.date_end,
                beginning,
                ending,
            )

            response = reconciliation_from_row(row)

            await write_activity_log(
                conn, auth_user.id, "reconciliation", str(row["id"]), 1,
                after_snapshot=response,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


# ---------------------------------------------------------------------------
# GET /reconciliations/{reconciliation_id}
# ---------------------------------------------------------------------------
@router.get("/{reconciliation_id}")
async def get_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    debit_as_negative: bool = Query(False),
):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            reconciliation_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("reconciliation")

        txn_rows = await conn.fetch(
            """
            SELECT * FROM expense_transactions
            WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
            ORDER BY date DESC, created_at DESC
            """,
            reconciliation_id,
            auth_user.id,
        )

        recon = reconciliation_from_row(row)
        txns = [transaction_from_row(r) for r in txn_rows]
        if debit_as_negative:
            txns = [_apply_debit_as_negative(t) for t in txns]

        return {**recon, "transactions": txns}


# ---------------------------------------------------------------------------
# PUT /reconciliations/{reconciliation_id}
# ---------------------------------------------------------------------------
@router.put("/{reconciliation_id}")
async def update_reconciliation(
    reconciliation_id: str,
    body: ReconciliationUpdateRequest,
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
                "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                reconciliation_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("reconciliation")
            return reconciliation_from_row(row)

        # Validate name if changing
        if "name" in fields:
            if not fields["name"] or not fields["name"].strip():
                raise validation_error(
                    "Name must not be empty.",
                    {"name": "Must not be empty."},
                )
            fields["name"] = fields["name"].strip()

        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                reconciliation_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("reconciliation")

            before = reconciliation_from_row(before_row)

            set_clauses = []
            params = [reconciliation_id, auth_user.id]
            for i, (key, value) in enumerate(fields.items(), start=3):
                set_clauses.append(f"{key} = ${i}")
                params.append(value)
            set_clauses.append("updated_at = now()")
            set_clauses.append("version = version + 1")

            query = f"""
                UPDATE expense_reconciliations
                SET {', '.join(set_clauses)}
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING *
            """
            after_row = await conn.fetchrow(query, *params)
            if after_row is None:
                raise not_found("reconciliation")

            after = reconciliation_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "reconciliation", reconciliation_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


# ---------------------------------------------------------------------------
# POST /reconciliations/{reconciliation_id}/complete
# ---------------------------------------------------------------------------
@router.post("/{reconciliation_id}/complete")
async def complete_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                reconciliation_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("reconciliation")

            # Already completed — return idempotently
            if row["status"] == 2:
                return reconciliation_from_row(row)

            # Must have at least one assigned transaction
            txn_count = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transactions
                WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
                """,
                reconciliation_id,
                auth_user.id,
            )
            if txn_count == 0:
                raise validation_error(
                    "Cannot complete reconciliation with no assigned transactions.",
                    {"transactions": "At least one transaction must be assigned."},
                )

            before = reconciliation_from_row(row)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_reconciliations
                SET status = 2, updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                reconciliation_id,
                auth_user.id,
            )

            after = reconciliation_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "reconciliation", reconciliation_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


# ---------------------------------------------------------------------------
# POST /reconciliations/{reconciliation_id}/revert
# ---------------------------------------------------------------------------
@router.post("/{reconciliation_id}/revert")
async def revert_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                reconciliation_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("reconciliation")

            # Already draft — return idempotently
            if row["status"] == 1:
                return reconciliation_from_row(row)

            before = reconciliation_from_row(row)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_reconciliations
                SET status = 1, updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                reconciliation_id,
                auth_user.id,
            )

            after = reconciliation_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "reconciliation", reconciliation_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


# ---------------------------------------------------------------------------
# DELETE /reconciliations/{reconciliation_id}
# ---------------------------------------------------------------------------
@router.delete("/{reconciliation_id}")
async def delete_reconciliation(
    reconciliation_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        row = await conn.fetchrow(
            "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            reconciliation_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("reconciliation")

        if row["status"] == 2:
            raise conflict("Cannot delete a completed reconciliation. Revert to draft first.")

        async with conn.transaction():
            before = reconciliation_from_row(row)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_reconciliations
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                reconciliation_id,
                auth_user.id,
            )

            after = reconciliation_from_row(after_row)

            # Unassign all transactions from this batch
            await conn.execute(
                """
                UPDATE expense_transactions
                SET reconciliation_id = NULL, updated_at = now(), version = version + 1
                WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
                """,
                reconciliation_id,
                auth_user.id,
            )

            await write_activity_log(
                conn, auth_user.id, "reconciliation", reconciliation_id, 3,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after
