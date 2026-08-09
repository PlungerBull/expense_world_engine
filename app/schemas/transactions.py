from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.constants import TransactionType
from app.schemas import StrictModel


class TransferField(StrictModel):
    id: UUID  # sibling transaction's client-supplied uuid
    account_id: str
    amount_cents: int  # signed: negative=outflow, positive=inflow


class TransactionCreateRequest(StrictModel):
    # Unknown fields 422 rather than being silently dropped. This is what makes
    # the removal of `exchange_rate` (sql/021) visible to a client still sending
    # it: the engine no longer stores a rate anywhere, and a caller who believes
    # the value matters deserves to be told it does not.
    id: UUID
    title: str
    amount_cents: int  # signed: negative=expense, positive=income
    date: AwareDatetime
    account_id: str
    # Required for normal transactions, ignored for transfers (the engine
    # auto-assigns @Transfer/@Debt). Conditional requirement is enforced in
    # create_transaction, not here, since it depends on the transfer field.
    category_id: Optional[str] = None
    description: Optional[str] = None
    cleared: Optional[bool] = None
    hashtag_ids: Optional[list[str]] = None
    transfer: Optional[TransferField] = None


class TransactionUpdateRequest(StrictModel):
    title: Optional[str] = None
    amount_cents: Optional[int] = None  # signed: negative=expense, positive=income
    date: Optional[AwareDatetime] = None
    account_id: Optional[str] = None
    category_id: Optional[str] = None
    description: Optional[str] = None
    cleared: Optional[bool] = None
    hashtag_ids: Optional[list[str]] = None
    reconciliation_id: Optional[str] = None


class TransactionBatchRequest(StrictModel):
    transactions: list[TransactionCreateRequest]


class TransactionResponse(BaseModel):
    id: str
    user_id: str
    title: str
    description: Optional[str] = None
    # Native only. A transaction belongs to one account and the account governs
    # the currency, so a home-currency figure on the row would be a second copy
    # of a number nothing here combines. Conversion happens where currencies are
    # combined — the monthly report and its totals — and nowhere else.
    # Absent, not null: a permanently-null key on every transaction forever is
    # dead weight, and this is the documented exception to null-over-omission.
    amount_cents: int
    # Direction, and nothing else — a transfer is identified by
    # transfer_transaction_id, not by a third type value. Typed with the enum
    # (wire-identical plain int, OpenAPI documents the closed set); safe to
    # fail a read loudly because sql/020 CHECKs the column.
    transaction_type: TransactionType
    date: datetime
    account_id: str
    category_id: str
    cleared: bool
    transfer_transaction_id: Optional[str] = None
    inbox_id: Optional[str] = None
    reconciliation_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int
    deleted_at: Optional[datetime] = None
    # Per api-design-principles.md §3a — junction tables are storage,
    # the wire format flattens to an embedded array on every transaction
    # representation returned by any read endpoint.
    hashtag_ids: list[str] = Field(default_factory=list)


class TransactionWithWarningsResponse(TransactionResponse):
    """DELETE /transactions/{id} and POST /transactions/{id}/restore only.

    ``warnings`` carries side-effect notes (reconciliation unlink on a
    completed reconciliation). Required, no default: those two routes always
    emit it — empty when the operation is clean — and a path that forgot to
    set it should fail response validation, not be papered over. Deliberately
    NOT on other mutations: the key exists where a warning can actually occur
    (owner decision 2026-08-07, D9 in open-bugs.md).
    """

    warnings: list[str]


class TransactionBatchResponse(BaseModel):
    created: list[TransactionResponse]


def transaction_from_row(row, hashtag_ids: Optional[list[str]] = None) -> dict:
    """Serialize a transaction row.

    ``hashtag_ids`` is the resolved set of hashtag UUIDs to attach to the
    response. Callers that surface this dict on the wire — or persist it
    as an activity-log snapshot — MUST pass the actual list (see §3a /
    §6 aggregate exception #1). When omitted, the field defaults to ``[]``.

    The row may also carry a pre-aggregated ``hashtag_ids`` column (this
    is how the list endpoints supply the array via in-query ``array_agg``).
    An explicit ``hashtag_ids`` argument takes precedence over a column
    of the same name on the row.
    """
    resolved: list[str]
    if hashtag_ids is not None:
        resolved = [str(h) for h in hashtag_ids]
    else:
        try:
            row_value = row["hashtag_ids"]
        except (KeyError, TypeError):
            row_value = None
        resolved = [str(h) for h in row_value] if row_value else []

    return TransactionResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=row["title"],
        description=row["description"],
        amount_cents=row["amount_cents"],
        transaction_type=row["transaction_type"],
        date=row["date"],
        account_id=str(row["account_id"]),
        category_id=str(row["category_id"]),
        cleared=row["cleared"],
        transfer_transaction_id=str(row["transfer_transaction_id"]) if row["transfer_transaction_id"] else None,
        inbox_id=str(row["inbox_id"]) if row["inbox_id"] else None,
        reconciliation_id=str(row["reconciliation_id"]) if row["reconciliation_id"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        deleted_at=row["deleted_at"],
        hashtag_ids=resolved,
    ).model_dump(mode="json")


def infer_transaction_type(amount_cents: int) -> TransactionType:
    """Infer direction from a signed request amount.

    Negative = OUTFLOW (money leaves the account), positive = INFLOW.

    **Signs are read only in this module** — this function for direction,
    ``opposite_signs`` below for the transfer pairing rule. Every write
    path — ordinary transactions, batch, both transfer legs, and the
    inbox — routes through here, so there is exactly one answer to "what
    does the sign mean". Adding a reader anywhere else is the bug, not
    the fix.

    Until WP1 there was a second, byte-identical copy of this rule named
    ``infer_transfer_direction``, because transfers encoded their direction in
    a separate column. That column is gone (sql/020) and so is the duplicate.

    Callers must reject zero first; ``0`` maps to INFLOW here.
    """
    return TransactionType.OUTFLOW if amount_cents < 0 else TransactionType.INFLOW


MSG_OPPOSITE_SIGN = "Must have opposite sign to amount_cents."


def opposite_signs(a: int, b: int) -> bool:
    """True when the two signed amounts point opposite ways.

    The transfer pairing rule: one leg flows out, the other flows in.
    Non-raising by design — the ledger path accumulates the failure into
    its errors dict (multi-error responses are pinned) while the inbox
    path raises immediately; both report ``MSG_OPPOSITE_SIGN``.

    Callers must reject zero first; zero has no sign.
    """
    return (a > 0) != (b > 0)
