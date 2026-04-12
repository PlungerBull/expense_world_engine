from typing import Optional

from pydantic import BaseModel

from app.schemas.accounts import AccountResponse
from app.schemas.auth import UserSettingsResponse
from app.schemas.categories import CategoryResponse
from app.schemas.hashtags import HashtagResponse
from app.schemas.inbox import InboxResponse
from app.schemas.reconciliations import ReconciliationResponse
from app.schemas.transactions import TransactionResponse


class TransactionSyncRow(TransactionResponse):
    """Transaction wire shape with hashtag_ids embedded.

    Junction table (`expense_transaction_hashtags`) is internal storage; clients
    only see the flattened `hashtag_ids` array. Sorted ascending for stable
    output and easier client-side diffing.
    """

    hashtag_ids: list[str] = []


class SyncResponse(BaseModel):
    sync_token: str
    accounts: list[AccountResponse]
    categories: list[CategoryResponse]
    hashtags: list[HashtagResponse]
    inbox: list[InboxResponse]
    transactions: list[TransactionSyncRow]
    reconciliations: list[ReconciliationResponse]
    settings: Optional[UserSettingsResponse]
