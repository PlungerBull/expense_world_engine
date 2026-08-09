from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import AwareDatetime, BaseModel

from app.schemas import StrictModel, audit_fields, owned_fields


class AccountCreateRequest(StrictModel):
    # Unknown fields (including is_person) 422 via StrictModel — person
    # accounts are created via the dedicated People API, never through this
    # endpoint.
    id: UUID
    name: str
    currency_code: str
    color: Optional[str] = None
    sort_order: Optional[int] = None


class OpeningBalanceRequest(StrictModel):
    # The transaction id is client-supplied (UUID-first convention) so bulk
    # importers get deterministic dedup on re-runs.
    transaction_id: UUID
    amount_cents: int  # signed: positive = money you had, negative = starting debt
    date: AwareDatetime
    title: Optional[str] = None  # defaults to "Opening balance"


class AccountUpdateRequest(StrictModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None
    currency_code: Optional[str] = None  # accepted but rejected at service level


class AccountResponse(BaseModel):
    id: str
    user_id: str
    name: str
    currency_code: str
    is_person: bool
    color: str
    current_balance_cents: int
    current_balance_home_cents: Optional[int]
    is_archived: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None


def account_from_row(
    row,
    balance_cents: int,
    balance_home_cents: Optional[int] = None,
) -> dict:
    """Serialize an account row. The balance is supplied, never read off the row.

    ``balance_cents`` is a required positional because sql/022 dropped the stored
    column: the balance is now computed from the ledger
    (``helpers/account_balance``) and there is nothing on ``row`` to fall back
    to. Making it required means a caller that forgets it raises ``TypeError``
    rather than emitting a confident zero, which on a balance is
    indistinguishable from an empty account.

    ``balance_home_cents`` stays optional and defaults to ``None`` because
    ``null`` is its real meaning: no rate was available for the account's
    currency today.
    """
    return AccountResponse(
        **owned_fields(row),
        name=row["name"],
        currency_code=row["currency_code"],
        is_person=row["is_person"],
        color=row["color"],
        current_balance_cents=balance_cents,
        current_balance_home_cents=balance_home_cents,
        is_archived=row["is_archived"],
        sort_order=row["sort_order"],
        **audit_fields(row),
    ).model_dump(mode="json")
