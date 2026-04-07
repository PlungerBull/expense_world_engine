from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.exchange_rate import lookup_exchange_rate
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.helpers.pagination import clamp_limit, paginated_response
from app.schemas.inbox import InboxCreateRequest, InboxResponse, InboxUpdateRequest
from app.schemas.transactions import transaction_from_row, infer_transaction_type

router = APIRouter(prefix="/inbox", tags=["inbox"])


def _inbox_from_row(row) -> dict:
    return InboxResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=row["title"],
        description=row["description"],
        amount_cents=row["amount_cents"],
        transaction_type=row["transaction_type"],
        date=row["date"],
        account_id=str(row["account_id"]) if row["account_id"] else None,
        category_id=str(row["category_id"]) if row["category_id"] else None,
        exchange_rate=float(row["exchange_rate"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")




# ---------------------------------------------------------------------------
# GET /inbox
# ---------------------------------------------------------------------------
@router.get("")
async def list_inbox(
    auth_user: CurrentUser,
    ready: bool = Query(False),
    overdue: bool = Query(False),
    include_deleted: bool = Query(False),
    limit: int = Query(50),
    offset: int = Query(0),
):
    limit = clamp_limit(limit)

    async with db.pool.acquire() as conn:
        conditions = ["i.user_id = $1", "i.status = 1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("i.deleted_at IS NULL")

        if ready:
            conditions.append("i.title IS NOT NULL")
            conditions.append("i.title != 'UNTITLED'")
            conditions.append("i.amount_cents IS NOT NULL")
            conditions.append("i.amount_cents != 0")
            conditions.append("i.date IS NOT NULL")
            conditions.append("i.date <= now()")
            conditions.append("i.account_id IS NOT NULL")
            conditions.append("i.category_id IS NOT NULL")
            # Account must be active and non-archived
            conditions.append(
                "EXISTS (SELECT 1 FROM expense_bank_accounts a "
                "WHERE a.id = i.account_id AND a.deleted_at IS NULL AND a.is_archived = false)"
            )
            # Category must be active
            conditions.append(
                "EXISTS (SELECT 1 FROM expense_categories c "
                "WHERE c.id = i.category_id AND c.deleted_at IS NULL)"
            )

        if overdue:
            conditions.append("i.date IS NOT NULL")
            conditions.append("i.date < now()")

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_transaction_inbox i WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT i.* FROM expense_transaction_inbox i
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [_inbox_from_row(row) for row in rows]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /inbox
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
async def create_inbox_item(
    body: InboxCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        # Infer transaction_type and normalize amount
        amount_cents = body.amount_cents
        transaction_type = None
        if amount_cents is not None:
            if amount_cents == 0:
                raise validation_error(
                    "amount_cents must not be zero.",
                    {"amount_cents": "Must not be zero."},
                )
            transaction_type = infer_transaction_type(amount_cents)
            amount_cents = abs(amount_cents)

        # Auto-populate exchange_rate if both account_id and date are present
        exchange_rate = body.exchange_rate
        if exchange_rate is None and body.account_id and body.date:
            exchange_rate = await lookup_exchange_rate(conn, body.account_id, body.date, auth_user.id)

        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO expense_transaction_inbox
                    (user_id, title, description, amount_cents, transaction_type,
                     date, account_id, category_id, exchange_rate, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, 1.0), now(), now())
                RETURNING *
                """,
                auth_user.id,
                body.title,
                body.description,
                amount_cents,
                transaction_type,
                body.date,
                body.account_id,
                body.category_id,
                exchange_rate,
            )

            response = _inbox_from_row(row)

            await write_activity_log(
                conn, auth_user.id, "inbox", str(row["id"]), 1,
                after_snapshot=response,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


# ---------------------------------------------------------------------------
# GET /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.get("/{inbox_id}")
async def get_inbox_item(inbox_id: str, auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            inbox_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("inbox item")
        return _inbox_from_row(row)


# ---------------------------------------------------------------------------
# PUT /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.put("/{inbox_id}")
async def update_inbox_item(
    inbox_id: str,
    body: InboxUpdateRequest,
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
                "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                inbox_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("inbox item")
            return _inbox_from_row(row)

        # Process amount_cents: infer transaction_type, normalize to abs
        if "amount_cents" in fields:
            if fields["amount_cents"] == 0:
                raise validation_error(
                    "amount_cents must not be zero.",
                    {"amount_cents": "Must not be zero."},
                )
            fields["transaction_type"] = infer_transaction_type(fields["amount_cents"])
            fields["amount_cents"] = abs(fields["amount_cents"])

        async with conn.transaction():
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                inbox_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("inbox item")

            before = _inbox_from_row(before_row)

            # Auto-populate exchange_rate if date changes and account_id is set
            # (unless user explicitly supplied exchange_rate in this request)
            if "date" in fields and "exchange_rate" not in fields:
                account_id = fields.get("account_id") or (
                    str(before_row["account_id"]) if before_row["account_id"] else None
                )
                if account_id and fields["date"]:
                    fields["exchange_rate"] = await lookup_exchange_rate(
                        conn, account_id, fields["date"], auth_user.id
                    )

            # Build dynamic UPDATE
            set_clauses = []
            params = [inbox_id, auth_user.id]
            for i, (key, value) in enumerate(fields.items(), start=3):
                set_clauses.append(f"{key} = ${i}")
                params.append(value)
            set_clauses.append("updated_at = now()")
            set_clauses.append("version = version + 1")

            query = f"""
                UPDATE expense_transaction_inbox
                SET {', '.join(set_clauses)}
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING *
            """
            after_row = await conn.fetchrow(query, *params)
            if after_row is None:
                raise not_found("inbox item")

            after = _inbox_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "inbox", inbox_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


# ---------------------------------------------------------------------------
# DELETE /inbox/{inbox_id}
# ---------------------------------------------------------------------------
@router.delete("/{inbox_id}")
async def delete_inbox_item(
    inbox_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        row = await conn.fetchrow(
            "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            inbox_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("inbox item")

        async with conn.transaction():
            before = _inbox_from_row(row)

            after_row = await conn.fetchrow(
                """
                UPDATE expense_transaction_inbox
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                inbox_id,
                auth_user.id,
            )
            after = _inbox_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "inbox", inbox_id, 3,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


# ---------------------------------------------------------------------------
# POST /inbox/{inbox_id}/promote
# ---------------------------------------------------------------------------
@router.post("/{inbox_id}/promote")
async def promote_inbox_item(
    inbox_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            # 1. Fetch inbox item
            inbox_row = await conn.fetchrow(
                """
                SELECT * FROM expense_transaction_inbox
                WHERE id = $1 AND user_id = $2 AND status = 1 AND deleted_at IS NULL
                """,
                inbox_id,
                auth_user.id,
            )
            if inbox_row is None:
                raise not_found("inbox item")

            inbox_before = _inbox_from_row(inbox_row)

            # 2. Validate all required fields — collect all failures
            errors: dict = {}

            if not inbox_row["title"] or inbox_row["title"] == "UNTITLED":
                errors["title"] = "Must be present and not 'UNTITLED'."

            if inbox_row["amount_cents"] is None or inbox_row["amount_cents"] == 0:
                errors["amount_cents"] = "Must be present and not zero."

            if inbox_row["date"] is None:
                errors["date"] = "Must be present and not in the future."
            elif inbox_row["date"] > await conn.fetchval("SELECT now()"):
                errors["date"] = "Must be present and not in the future."

            if inbox_row["account_id"] is None:
                errors["account_id"] = "Must reference an active, non-archived account."
            else:
                account = await conn.fetchrow(
                    """
                    SELECT id, currency_code FROM expense_bank_accounts
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
                    """,
                    inbox_row["account_id"],
                    auth_user.id,
                )
                if account is None:
                    errors["account_id"] = "Must reference an active, non-archived account."

            if inbox_row["category_id"] is None:
                errors["category_id"] = "Must reference an active category."
            else:
                category = await conn.fetchrow(
                    "SELECT id FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                    inbox_row["category_id"],
                    auth_user.id,
                )
                if category is None:
                    errors["category_id"] = "Must reference an active category."

            if errors:
                raise validation_error("Inbox item is not ready to promote.", errors)

            # 3. Determine transaction_type — use stored value, or default to expense
            transaction_type = inbox_row["transaction_type"] or 1

            # 4. Compute amount_home_cents
            exchange_rate = float(inbox_row["exchange_rate"])
            amount_home_cents = round(inbox_row["amount_cents"] * exchange_rate)

            # 5. Create expense_transactions row
            txn_row = await conn.fetchrow(
                """
                INSERT INTO expense_transactions
                    (user_id, title, description, amount_cents, amount_home_cents,
                     transaction_type, date, account_id, category_id, exchange_rate,
                     inbox_id, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), now())
                RETURNING *
                """,
                auth_user.id,
                inbox_row["title"],
                inbox_row["description"],
                inbox_row["amount_cents"],
                amount_home_cents,
                transaction_type,
                inbox_row["date"],
                inbox_row["account_id"],
                inbox_row["category_id"],
                inbox_row["exchange_rate"],
                inbox_row["id"],
            )

            txn_response = transaction_from_row(txn_row)

            # 6. Update inbox row: status=2 (promoted), soft-delete
            inbox_after_row = await conn.fetchrow(
                """
                UPDATE expense_transaction_inbox
                SET status = 2, deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                inbox_id,
                auth_user.id,
            )
            inbox_after = _inbox_from_row(inbox_after_row)

            # 7. Update account balance based on transaction_type
            if transaction_type == 1:  # expense
                await conn.execute(
                    """
                    UPDATE expense_bank_accounts
                    SET current_balance_cents = current_balance_cents - $1,
                        updated_at = now(), version = version + 1
                    WHERE id = $2 AND user_id = $3
                    """,
                    inbox_row["amount_cents"],
                    inbox_row["account_id"],
                    auth_user.id,
                )
            elif transaction_type == 2:  # income
                await conn.execute(
                    """
                    UPDATE expense_bank_accounts
                    SET current_balance_cents = current_balance_cents + $1,
                        updated_at = now(), version = version + 1
                    WHERE id = $2 AND user_id = $3
                    """,
                    inbox_row["amount_cents"],
                    inbox_row["account_id"],
                    auth_user.id,
                )

            # 8. Activity log: transaction created
            await write_activity_log(
                conn, auth_user.id, "transaction", str(txn_row["id"]), 1,
                after_snapshot=txn_response,
            )

            # 9. Activity log: inbox item deleted
            await write_activity_log(
                conn, auth_user.id, "inbox", inbox_id, 3,
                before_snapshot=inbox_before,
                after_snapshot=inbox_after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, txn_response)
        return txn_response
