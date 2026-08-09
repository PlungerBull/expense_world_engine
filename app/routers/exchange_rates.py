from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser, Limit, Offset
from app.errors import ERROR_RESPONSES, not_found, validation_error
from app.helpers.exchange_rate import get_rate, rate_lookup_date
from app.helpers.pagination import DEFAULT_LIMIT, paginated_response
from app.helpers.validation import currency_code_error
from app.schemas.exchange_rates import ExchangeRateHistoryItem, ExchangeRateResponse
from app.schemas.pagination import Paginated

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"], responses=ERROR_RESPONSES)


@router.get("", response_model=ExchangeRateResponse)
async def get_exchange_rate(
    auth_user: CurrentUser,
    target: str = Query(..., min_length=3, max_length=3),
    base: str = Query("USD", min_length=3, max_length=3),
    date: Optional[date_type] = Query(None),
):
    base_upper = base.upper()
    target_upper = target.upper()

    async with db.pool.acquire() as conn:
        # An unsupported currency is a bad *input*, not a missing *resource* —
        # same 422 the write path gives it (create_account). 404 below is
        # reserved for a supported pair with genuinely no rate row.
        errors = {}
        for field, code in (("base", base_upper), ("target", target_upper)):
            message = await currency_code_error(conn, code)
            if message is not None:
                errors[field] = message
        if errors:
            raise validation_error("Invalid currency code.", errors)

        if date is None:
            # Default "today" resolves in the user's display_timezone, like
            # every other current-date rate lookup. Tolerate a missing
            # settings row — a rate lookup is not a report; 422ing it here
            # would be a second, larger behavior change.
            tz_row = await conn.fetchrow(
                "SELECT display_timezone FROM user_settings WHERE user_id = $1",
                auth_user.id,
            )
            target_date = rate_lookup_date(
                tz_row["display_timezone"] if tz_row else "UTC"
            )
        else:
            target_date = date
        result = await get_rate(
            conn,
            from_currency=base_upper,
            to_currency=target_upper,
            as_of=target_date,
        )

    if result is None:
        raise not_found(f"exchange rate for {base_upper}->{target_upper}")

    rate, rate_date = result
    return ExchangeRateResponse(
        base=base_upper,
        target=target_upper,
        date=target_date,
        rate_date=rate_date,
        rate=rate,
    ).model_dump(mode="json")


@router.get("/history", response_model=Paginated[ExchangeRateHistoryItem])
async def list_exchange_rate_history(
    auth_user: CurrentUser,
    date: Optional[date_type] = Query(None),
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
):
    """List stored exchange-rate rows, newest first.

    Unlike the lookup above, this has no fallback semantics: it returns
    exactly the rows that exist. A date with no rows is an empty page,
    not an error. UNIQUE (base_currency, target_currency, rate_date)
    guarantees one row per pair per day, so no server-side dedup is needed.
    """
    conditions = []
    params: list = []

    if date is not None:
        conditions.append(f"rate_date = ${len(params) + 1}")
        params.append(date)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with db.pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM exchange_rates {where}", *params
        )

        rows = await conn.fetch(
            f"""
            SELECT base_currency, target_currency, rate, rate_date
            FROM exchange_rates
            {where}
            ORDER BY rate_date DESC, base_currency ASC, target_currency ASC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )

    items = [
        ExchangeRateHistoryItem(
            base=row["base_currency"],
            target=row["target_currency"],
            rate_date=row["rate_date"],
            rate=float(row["rate"]),
        ).model_dump(mode="json")
        for row in rows
    ]
    return paginated_response(items, total, limit, offset)
