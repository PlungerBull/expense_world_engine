from datetime import datetime
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
from app.schemas.transactions import (
    TransactionBatchRequest,
    TransactionCreateRequest,
    TransactionUpdateRequest,
    infer_transaction_type,
    transaction_from_row,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])


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


async def _update_account_balance(conn, account_id, user_id, amount_cents, transaction_type, transfer_direction=None):
    """Apply a transaction's balance contribution to its account."""
    if transaction_type == 1:  # expense — subtract
        delta = -amount_cents
    elif transaction_type == 2:  # income — add
        delta = amount_cents
    elif transaction_type == 3:  # transfer — direction from transfer_direction
        if transfer_direction == 1:  # debit — subtract
            delta = -amount_cents
        elif transfer_direction == 2:  # credit — add
            delta = amount_cents
        else:
            return
    else:
        return
    await conn.execute(
        """
        UPDATE expense_bank_accounts
        SET current_balance_cents = current_balance_cents + $1,
            updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        """,
        delta,
        account_id,
        user_id,
    )


async def _reverse_account_balance(conn, account_id, user_id, amount_cents, transaction_type, transfer_direction=None):
    """Reverse a transaction's balance contribution (for delete/update corrections)."""
    if transaction_type == 1:  # was expense — add back
        delta = amount_cents
    elif transaction_type == 2:  # was income — subtract
        delta = -amount_cents
    elif transaction_type == 3:  # was transfer — reverse direction
        if transfer_direction == 1:  # was debit — add back
            delta = amount_cents
        elif transfer_direction == 2:  # was credit — subtract
            delta = -amount_cents
        else:
            return
    else:
        return
    await conn.execute(
        """
        UPDATE expense_bank_accounts
        SET current_balance_cents = current_balance_cents + $1,
            updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        """,
        delta,
        account_id,
        user_id,
    )


async def _sync_hashtags(conn, transaction_id, user_id, hashtag_ids):
    """Validate hashtags and replace junction rows for a ledger transaction."""
    if hashtag_ids:
        valid = await conn.fetch(
            "SELECT id FROM expense_hashtags WHERE id = ANY($1::uuid[]) AND user_id = $2 AND deleted_at IS NULL",
            hashtag_ids,
            user_id,
        )
        valid_ids = {str(r["id"]) for r in valid}
        invalid = [h for h in hashtag_ids if h not in valid_ids]
        if invalid:
            raise validation_error(
                "Some hashtag IDs are invalid.",
                {"hashtag_ids": f"Invalid IDs: {', '.join(invalid)}"},
            )

    # Soft-delete existing junction rows
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now(), version = version + 1
        WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
        """,
        transaction_id,
        user_id,
    )

    # Insert new rows
    if hashtag_ids:
        for h_id in hashtag_ids:
            await conn.execute(
                """
                INSERT INTO expense_transaction_hashtags
                    (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
                VALUES ($1, 1, $2, $3, now(), now())
                """,
                transaction_id,
                h_id,
                user_id,
            )


# ---------------------------------------------------------------------------
# GET /transactions/{transaction_id}
# ---------------------------------------------------------------------------
@router.get("/{transaction_id}")
async def get_transaction(
    transaction_id: str,
    auth_user: CurrentUser,
    debit_as_negative: bool = Query(False),
):
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            transaction_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("transaction")
        data = transaction_from_row(row)
        if debit_as_negative:
            data = _apply_debit_as_negative(data)
        return data


# ---------------------------------------------------------------------------
# GET /transactions
# ---------------------------------------------------------------------------
@router.get("")
async def list_transactions(
    auth_user: CurrentUser,
    account_id: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    hashtag_id: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    cleared: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    include_deleted: bool = Query(False),
    debit_as_negative: bool = Query(False),
    limit: int = Query(50),
    offset: int = Query(0),
):
    limit = clamp_limit(limit)

    async with db.pool.acquire() as conn:
        conditions = ["t.user_id = $1"]
        params: list = [auth_user.id]

        if not include_deleted:
            conditions.append("t.deleted_at IS NULL")

        if account_id:
            params.append(account_id)
            conditions.append(f"t.account_id = ${len(params)}")

        if category_id:
            params.append(category_id)
            conditions.append(f"t.category_id = ${len(params)}")

        if hashtag_id:
            params.append(hashtag_id)
            conditions.append(
                f"EXISTS (SELECT 1 FROM expense_transaction_hashtags th "
                f"WHERE th.transaction_id = t.id AND th.hashtag_id = ${len(params)} "
                f"AND th.deleted_at IS NULL)"
            )

        if date_from:
            params.append(date_from)
            conditions.append(f"t.date >= ${len(params)}")

        if date_to:
            params.append(date_to)
            conditions.append(f"t.date <= ${len(params)}")

        if cleared is not None:
            params.append(cleared)
            conditions.append(f"t.cleared = ${len(params)}")

        if search:
            pattern = f"%{search}%"
            params.append(pattern)
            conditions.append(
                f"(t.title ILIKE ${len(params)} OR t.description ILIKE ${len(params)})"
            )

        where = " AND ".join(conditions)

        total = await conn.fetchval(
            f"SELECT count(*) FROM expense_transactions t WHERE {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT t.* FROM expense_transactions t
            WHERE {where}
            ORDER BY t.date DESC, t.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

        data = [transaction_from_row(row) for row in rows]
        if debit_as_negative:
            data = [_apply_debit_as_negative(d) for d in data]
        return paginated_response(data, total, limit, offset)


# ---------------------------------------------------------------------------
# POST /transactions
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
async def create_transaction(
    body: TransactionCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        async with conn.transaction():
            # Validate shared fields — collect all failures
            errors: dict = {}

            if not body.title or not body.title.strip():
                errors["title"] = "Must not be empty."

            if body.amount_cents == 0:
                errors["amount_cents"] = "Must not be zero."

            # Date must be <= now()
            now = await conn.fetchval("SELECT now()")
            if body.date > now:
                errors["date"] = "Must not be in the future."

            if errors:
                raise validation_error("Transaction validation failed.", errors)

            # ----- Transfer branch -----
            if body.transfer is not None:
                from app.helpers.transfers import create_transfer_pair

                primary_response, _sibling = await create_transfer_pair(
                    conn=conn,
                    user_id=auth_user.id,
                    primary_title=body.title.strip(),
                    primary_description=body.description,
                    primary_amount_cents=body.amount_cents,
                    primary_account_id=body.account_id,
                    primary_date=body.date,
                    primary_exchange_rate=body.exchange_rate,
                    primary_cleared=body.cleared if body.cleared is not None else False,
                    transfer_account_id=body.transfer.account_id,
                    transfer_amount_cents=body.transfer.amount_cents,
                )

                # Hashtags on primary only
                if body.hashtag_ids:
                    await _sync_hashtags(
                        conn, primary_response["id"], auth_user.id, body.hashtag_ids,
                    )

                response = primary_response

                await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
                return JSONResponse(content=response, status_code=201)

            # ----- Normal (non-transfer) branch -----

            # Validate account_id — active, non-archived
            account = await conn.fetchrow(
                """
                SELECT id, currency_code FROM expense_bank_accounts
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
                """,
                body.account_id,
                auth_user.id,
            )
            if account is None:
                raise validation_error(
                    "Transaction validation failed.",
                    {"account_id": "Must reference an active, non-archived account."},
                )

            # Validate category_id — active
            category = await conn.fetchrow(
                "SELECT id FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                body.category_id,
                auth_user.id,
            )
            if category is None:
                raise validation_error(
                    "Transaction validation failed.",
                    {"category_id": "Must reference an active category."},
                )

            # Infer transaction_type and normalize amount
            transaction_type = infer_transaction_type(body.amount_cents)
            amount_cents = abs(body.amount_cents)

            # Exchange rate
            exchange_rate = body.exchange_rate
            if exchange_rate is None:
                exchange_rate = await lookup_exchange_rate(conn, body.account_id, body.date, auth_user.id)
            amount_home_cents = round(amount_cents * exchange_rate)

            # Insert
            row = await conn.fetchrow(
                """
                INSERT INTO expense_transactions
                    (user_id, title, description, amount_cents, amount_home_cents,
                     transaction_type, date, account_id, category_id, exchange_rate,
                     cleared, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), now())
                RETURNING *
                """,
                auth_user.id,
                body.title.strip(),
                body.description,
                amount_cents,
                amount_home_cents,
                transaction_type,
                body.date,
                body.account_id,
                body.category_id,
                exchange_rate,
                body.cleared if body.cleared is not None else False,
            )

            response = transaction_from_row(row)

            # Update account balance
            await _update_account_balance(conn, body.account_id, auth_user.id, amount_cents, transaction_type)

            # Hashtags
            if body.hashtag_ids:
                await _sync_hashtags(conn, str(row["id"]), auth_user.id, body.hashtag_ids)

            # Activity log
            await write_activity_log(
                conn, auth_user.id, "transaction", str(row["id"]), 1,
                after_snapshot=response,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return JSONResponse(content=response, status_code=201)


# ---------------------------------------------------------------------------
# PUT /transactions/{transaction_id}
# ---------------------------------------------------------------------------
@router.put("/{transaction_id}")
async def update_transaction(
    transaction_id: str,
    body: TransactionUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    fields = body.model_dump(exclude_none=True)
    hashtag_ids = fields.pop("hashtag_ids", None)

    # reconciliation_id needs special handling: model_dump(exclude_none=True) drops
    # null values, but we need to distinguish "omitted" from "explicitly set to null"
    # so clients can unassign by sending reconciliation_id: null.
    recon_id_provided = "reconciliation_id" in body.model_fields_set
    recon_id_value = body.reconciliation_id
    fields.pop("reconciliation_id", None)

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        # Empty update — return current
        if not fields and hashtag_ids is None and not recon_id_provided:
            row = await conn.fetchrow(
                "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                transaction_id,
                auth_user.id,
            )
            if row is None:
                raise not_found("transaction")
            return transaction_from_row(row)

        async with conn.transaction():
            # Fetch before-state
            before_row = await conn.fetchrow(
                "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                transaction_id,
                auth_user.id,
            )
            if before_row is None:
                raise not_found("transaction")

            before = transaction_from_row(before_row)

            # Field locking check — reconciliation completed
            if before_row["reconciliation_id"] is not None:
                recon = await conn.fetchrow(
                    "SELECT status FROM expense_reconciliations WHERE id = $1",
                    before_row["reconciliation_id"],
                )
                if recon and recon["status"] == 2:
                    locked = {"amount_cents", "account_id", "title", "date"}
                    attempted = locked & fields.keys()
                    if attempted:
                        raise validation_error(
                            "Transaction belongs to a completed reconciliation. These fields are locked.",
                            {f: "Locked by completed reconciliation." for f in attempted},
                        )

            # Validate reconciliation_id assignment
            if recon_id_provided and recon_id_value is not None:
                recon = await conn.fetchrow(
                    """
                    SELECT id, account_id, status FROM expense_reconciliations
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    """,
                    recon_id_value,
                    auth_user.id,
                )
                if recon is None:
                    raise validation_error(
                        "Reconciliation validation failed.",
                        {"reconciliation_id": "Must reference an active reconciliation."},
                    )
                effective_account_id = fields.get("account_id") or str(before_row["account_id"])
                if str(recon["account_id"]) != effective_account_id:
                    raise validation_error(
                        "Reconciliation validation failed.",
                        {"reconciliation_id": "Reconciliation account does not match transaction account."},
                    )
                if recon["status"] == 2:
                    raise validation_error(
                        "Reconciliation validation failed.",
                        {"reconciliation_id": "Cannot assign transactions to a completed reconciliation."},
                    )

            # Transfer edit guard — reject amount/account changes on transfers
            if before_row["transfer_transaction_id"] is not None:
                blocked = {"amount_cents", "account_id"} & fields.keys()
                if blocked:
                    raise validation_error(
                        "Transfer edits not yet supported.",
                        {f: "Cannot modify on a transfer transaction." for f in blocked},
                    )

            # Track whether balance needs updating
            needs_balance_update = False

            # Process amount_cents change
            if "amount_cents" in fields:
                if fields["amount_cents"] == 0:
                    raise validation_error(
                        "amount_cents must not be zero.",
                        {"amount_cents": "Must not be zero."},
                    )
                fields["transaction_type"] = infer_transaction_type(fields["amount_cents"])
                fields["amount_cents"] = abs(fields["amount_cents"])
                needs_balance_update = True

            # Process date change — re-fetch exchange rate (unless user provided one)
            if "date" in fields and "exchange_rate" not in fields:
                effective_account_id = fields.get("account_id") or str(before_row["account_id"])
                new_rate = await lookup_exchange_rate(conn, effective_account_id, fields["date"], auth_user.id)
                fields["exchange_rate"] = new_rate

            # Recalculate amount_home_cents when amount or exchange_rate changes
            if "amount_cents" in fields or "exchange_rate" in fields:
                effective_amount = fields.get("amount_cents", before_row["amount_cents"])
                effective_rate = fields.get("exchange_rate", float(before_row["exchange_rate"]))
                fields["amount_home_cents"] = round(effective_amount * effective_rate)

            # Validate new account_id if changing
            if "account_id" in fields:
                account = await conn.fetchrow(
                    """
                    SELECT id FROM expense_bank_accounts
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
                    """,
                    fields["account_id"],
                    auth_user.id,
                )
                if account is None:
                    raise validation_error(
                        "Account validation failed.",
                        {"account_id": "Must reference an active, non-archived account."},
                    )
                needs_balance_update = True

            # Validate new category_id if changing
            if "category_id" in fields:
                category = await conn.fetchrow(
                    "SELECT id FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                    fields["category_id"],
                    auth_user.id,
                )
                if category is None:
                    raise validation_error(
                        "Category validation failed.",
                        {"category_id": "Must reference an active category."},
                    )

            # Validate title if changing
            if "title" in fields and (not fields["title"] or not fields["title"].strip()):
                raise validation_error(
                    "Title validation failed.",
                    {"title": "Must not be empty."},
                )
            if "title" in fields:
                fields["title"] = fields["title"].strip()

            # Validate date if changing
            if "date" in fields:
                now = await conn.fetchval("SELECT now()")
                if fields["date"] > now:
                    raise validation_error(
                        "Date validation failed.",
                        {"date": "Must not be in the future."},
                    )

            # Balance update: reverse old, apply new
            if needs_balance_update:
                await _reverse_account_balance(
                    conn, str(before_row["account_id"]), auth_user.id,
                    before_row["amount_cents"], before_row["transaction_type"],
                    transfer_direction=before_row["transfer_direction"],
                )
                effective_account_id = fields.get("account_id") or str(before_row["account_id"])
                effective_amount = fields.get("amount_cents", before_row["amount_cents"])
                effective_type = fields.get("transaction_type", before_row["transaction_type"])
                await _update_account_balance(
                    conn, effective_account_id, auth_user.id,
                    effective_amount, effective_type,
                    transfer_direction=before_row["transfer_direction"],
                )

            # Build dynamic UPDATE
            if fields:
                set_clauses = []
                params = [transaction_id, auth_user.id]
                for i, (key, value) in enumerate(fields.items(), start=3):
                    set_clauses.append(f"{key} = ${i}")
                    params.append(value)
                set_clauses.append("updated_at = now()")
                set_clauses.append("version = version + 1")

                query = f"""
                    UPDATE expense_transactions
                    SET {', '.join(set_clauses)}
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    RETURNING *
                """
                after_row = await conn.fetchrow(query, *params)
                if after_row is None:
                    raise not_found("transaction")
            else:
                # Only hashtag changes, no column updates — still bump version
                after_row = await conn.fetchrow(
                    """
                    UPDATE expense_transactions
                    SET updated_at = now(), version = version + 1
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    RETURNING *
                    """,
                    transaction_id,
                    auth_user.id,
                )
                if after_row is None:
                    raise not_found("transaction")

            # Sync hashtags if provided
            if hashtag_ids is not None:
                await _sync_hashtags(conn, transaction_id, auth_user.id, hashtag_ids)

            # Apply reconciliation_id change
            if recon_id_provided:
                after_row = await conn.fetchrow(
                    """
                    UPDATE expense_transactions
                    SET reconciliation_id = $1, updated_at = now(), version = version + 1
                    WHERE id = $2 AND user_id = $3 AND deleted_at IS NULL
                    RETURNING *
                    """,
                    recon_id_value,
                    transaction_id,
                    auth_user.id,
                )

            after = transaction_from_row(after_row)

            # Activity log
            await write_activity_log(
                conn, auth_user.id, "transaction", transaction_id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after


# ---------------------------------------------------------------------------
# DELETE /transactions/{transaction_id}
# ---------------------------------------------------------------------------
@router.delete("/{transaction_id}")
async def delete_transaction(
    transaction_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        row = await conn.fetchrow(
            "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            transaction_id,
            auth_user.id,
        )
        if row is None:
            raise not_found("transaction")

        async with conn.transaction():
            before = transaction_from_row(row)

            # Soft-delete
            after_row = await conn.fetchrow(
                """
                UPDATE expense_transactions
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                transaction_id,
                auth_user.id,
            )
            after = transaction_from_row(after_row)

            # Reverse balance
            await _reverse_account_balance(
                conn, str(row["account_id"]), auth_user.id,
                row["amount_cents"], row["transaction_type"],
                transfer_direction=row["transfer_direction"],
            )

            # Soft-delete junction rows
            await conn.execute(
                """
                UPDATE expense_transaction_hashtags
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
                """,
                transaction_id,
                auth_user.id,
            )

            # Handle transfer sibling
            if row["transfer_transaction_id"] is not None:
                sibling_id = str(row["transfer_transaction_id"])
                sibling_row = await conn.fetchrow(
                    "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                    sibling_id,
                    auth_user.id,
                )
                if sibling_row is not None:
                    sibling_before = transaction_from_row(sibling_row)

                    sibling_after_row = await conn.fetchrow(
                        """
                        UPDATE expense_transactions
                        SET deleted_at = now(), updated_at = now(), version = version + 1
                        WHERE id = $1 AND user_id = $2
                        RETURNING *
                        """,
                        sibling_id,
                        auth_user.id,
                    )
                    sibling_after = transaction_from_row(sibling_after_row)

                    await _reverse_account_balance(
                        conn, str(sibling_row["account_id"]), auth_user.id,
                        sibling_row["amount_cents"], sibling_row["transaction_type"],
                        transfer_direction=sibling_row["transfer_direction"],
                    )

                    await conn.execute(
                        """
                        UPDATE expense_transaction_hashtags
                        SET deleted_at = now(), updated_at = now(), version = version + 1
                        WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
                        """,
                        sibling_id,
                        auth_user.id,
                    )

                    await write_activity_log(
                        conn, auth_user.id, "transaction", sibling_id, 3,
                        before_snapshot=sibling_before,
                        after_snapshot=sibling_after,
                    )

            # Activity log for primary transaction
            await write_activity_log(
                conn, auth_user.id, "transaction", transaction_id, 3,
                before_snapshot=before,
                after_snapshot=after,
            )

            # Reconciliation warning
            response = after
            if row["reconciliation_id"] is not None:
                recon = await conn.fetchrow(
                    "SELECT status FROM expense_reconciliations WHERE id = $1",
                    row["reconciliation_id"],
                )
                if recon and recon["status"] == 2:
                    response = {**after, "warning": "Transaction belonged to a completed reconciliation. Reconciliation totals may be stale."}

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


# ---------------------------------------------------------------------------
# POST /transactions/batch
# ---------------------------------------------------------------------------
@router.post("/batch", status_code=201)
async def batch_create_transactions(
    body: TransactionBatchRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    if not body.transactions:
        raise validation_error(
            "Batch must contain at least one transaction.",
            {"transactions": "Must not be empty."},
        )

    # Transfers are not supported in batch creates
    for i, item in enumerate(body.transactions):
        if item.transfer is not None:
            raise validation_error(
                "Transfers are not supported in batch creates.",
                {f"transactions[{i}].transfer": "Must not be present in batch."},
            )

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached, status_code=201)

        async with conn.transaction():
            now = await conn.fetchval("SELECT now()")

            # Pre-validate all items
            all_errors = []
            for i, item in enumerate(body.transactions):
                item_errors: dict = {}

                if not item.title or not item.title.strip():
                    item_errors["title"] = "Must not be empty."
                if item.amount_cents == 0:
                    item_errors["amount_cents"] = "Must not be zero."
                if item.date > now:
                    item_errors["date"] = "Must not be in the future."

                account = await conn.fetchrow(
                    """
                    SELECT id, currency_code FROM expense_bank_accounts
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
                    """,
                    item.account_id,
                    auth_user.id,
                )
                if account is None:
                    item_errors["account_id"] = "Must reference an active, non-archived account."

                category = await conn.fetchrow(
                    "SELECT id FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                    item.category_id,
                    auth_user.id,
                )
                if category is None:
                    item_errors["category_id"] = "Must reference an active category."

                if item_errors:
                    all_errors.append({"index": i, "fields": item_errors})

            if all_errors:
                raise validation_error(
                    "Batch validation failed.",
                    {"items": all_errors},
                )

            # Process all items — accumulate balance deltas
            created = []
            balance_deltas: dict[str, int] = {}

            for item in body.transactions:
                transaction_type = infer_transaction_type(item.amount_cents)
                amount_cents = abs(item.amount_cents)

                exchange_rate = item.exchange_rate
                if exchange_rate is None:
                    exchange_rate = await lookup_exchange_rate(conn, item.account_id, item.date, auth_user.id)
                amount_home_cents = round(amount_cents * exchange_rate)

                row = await conn.fetchrow(
                    """
                    INSERT INTO expense_transactions
                        (user_id, title, description, amount_cents, amount_home_cents,
                         transaction_type, date, account_id, category_id, exchange_rate,
                         cleared, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), now())
                    RETURNING *
                    """,
                    auth_user.id,
                    item.title.strip(),
                    item.description,
                    amount_cents,
                    amount_home_cents,
                    transaction_type,
                    item.date,
                    item.account_id,
                    item.category_id,
                    exchange_rate,
                    item.cleared if item.cleared is not None else False,
                )

                response = transaction_from_row(row)
                created.append(response)

                # Accumulate balance delta
                if transaction_type == 1:
                    delta = -amount_cents
                else:
                    delta = amount_cents
                balance_deltas[item.account_id] = balance_deltas.get(item.account_id, 0) + delta

                # Hashtags
                if item.hashtag_ids:
                    await _sync_hashtags(conn, str(row["id"]), auth_user.id, item.hashtag_ids)

                # Activity log
                await write_activity_log(
                    conn, auth_user.id, "transaction", str(row["id"]), 1,
                    after_snapshot=response,
                )

            # Apply accumulated balance deltas
            for acct_id, delta in balance_deltas.items():
                await conn.execute(
                    """
                    UPDATE expense_bank_accounts
                    SET current_balance_cents = current_balance_cents + $1,
                        updated_at = now(), version = version + 1
                    WHERE id = $2 AND user_id = $3
                    """,
                    delta,
                    acct_id,
                    auth_user.id,
                )

        batch_response = {"created": created}
        await store_idempotency(conn, auth_user.id, x_idempotency_key, batch_response)
        return JSONResponse(content=batch_response, status_code=201)
