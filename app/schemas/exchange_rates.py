from datetime import date as date_type

from pydantic import BaseModel


class ExchangeRateResponse(BaseModel):
    base: str
    target: str
    date: date_type
    rate_date: date_type
    rate: float
