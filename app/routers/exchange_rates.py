from datetime import date as date_type, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers.exchange_rate import get_rate
from app.schemas.exchange_rates import ExchangeRateResponse

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])


@router.get("")
async def get_exchange_rate(
    auth_user: CurrentUser,
    target: str = Query(..., min_length=3, max_length=3),
    base: str = Query("USD", min_length=3, max_length=3),
    date: Optional[date_type] = Query(None),
):
    target_date = date or datetime.now(timezone.utc).date()
    base_upper = base.upper()
    target_upper = target.upper()

    async with db.pool.acquire() as conn:
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
