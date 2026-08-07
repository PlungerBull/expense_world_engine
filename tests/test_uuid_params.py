"""Malformed UUID path/query params return 422, never 500 (bug 6.2).

Path and query parameters holding UUIDs are typed ``uuid.UUID`` in every
router, so FastAPI rejects garbage at the boundary with the standard
VALIDATION_ERROR envelope instead of letting it reach SQL and 500. One
route per router covers the shared pattern; ``/v1/activity?resource_id=``
was the pre-existing precedent (tests/test_phase_fixes.py).
"""
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, url, param",
    [
        ("GET", "/v1/accounts/not-a-uuid", "account_id"),
        ("GET", "/v1/categories/not-a-uuid", "category_id"),
        ("GET", "/v1/hashtags/not-a-uuid", "hashtag_id"),
        ("GET", "/v1/transactions/not-a-uuid", "transaction_id"),
        ("GET", "/v1/inbox/not-a-uuid", "inbox_id"),
        ("GET", "/v1/reconciliations/not-a-uuid", "reconciliation_id"),
        ("DELETE", "/v1/auth/pat/not-a-uuid", "pat_id"),
        ("GET", "/v1/transactions?account_id=not-a-uuid", "account_id"),
        ("GET", "/v1/transactions?category_id=not-a-uuid", "category_id"),
        ("GET", "/v1/transactions?hashtag_id=not-a-uuid", "hashtag_id"),
        ("GET", "/v1/transactions?reconciliation_id=not-a-uuid", "reconciliation_id"),
        ("GET", "/v1/reconciliations?account_id=not-a-uuid", "account_id"),
    ],
)
async def test_malformed_uuid_param_returns_422(client, method, url, param):
    r = await client.request(method, url)
    assert r.status_code == 422, (url, r.text)
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert param in (body.get("fields") or {}), (url, body)
