from datetime import date as date_type

from pydantic import BaseModel


# The two models deliberately restate each other rather than inheriting
# (bloat-audit §17e): `date` — the day the caller asked about, vs `rate_date`,
# the carry-forward row that answered — sits at position 3, and a subclass
# field would land last (key-order rule in schemas/__init__). Also a lookup
# result is-a history row only loosely; four shared lines don't buy that claim.
class ExchangeRateResponse(BaseModel):
    base: str
    target: str
    date: date_type
    rate_date: date_type
    rate: float


class ExchangeRateHistoryItem(BaseModel):
    base: str
    target: str
    rate_date: date_type
    rate: float
