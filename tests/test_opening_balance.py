"""POST /accounts/{id}/opening-balance — seed a balance via the @Opening system category.

Pins the contract:

  * Happy path: 201, seed transaction returned, @Opening auto-created
    (is_system, system_key="opening_balance"), account balance updated.
  * One active opening balance per account -> second POST is 409.
  * Duplicate client-supplied transaction_id -> 409.
  * 422: zero amount, future date, person account, unknown account.
  * Idempotency replay: same X-Idempotency-Key returns the stored response
    and the balance is applied exactly once.
  * Flow reports exclude the seed: no @Opening row in the categories panel,
    no contribution to inflow/outflow/net — and both exclusions survive
    renaming the category (they key off system_key, not the display name).
"""

import uuid
from typing import Optional

import pytest

from app import db

OPENING_KEY = "opening_balance"
PAST_DATE = "2026-04-12T12:00:00Z"


async def _make_account(user_id: str, balance_cents: int = 0) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, $3, 'PEN', false, '#00FF00',
                    $4, false, 9, now(), now())
            """,
            account_id, user_id, f"Opening-Test {uuid.uuid4().hex[:8]}", balance_cents,
        )
    return account_id


async def _make_person_account(user_id: str) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, $3, 'PEN', true, '#00FF00',
                    0, false, 9, now(), now())
            """,
            account_id, user_id, f"Opening-Person {uuid.uuid4().hex[:8]}",
        )
    return account_id


async def _soft_delete_account(account_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET deleted_at = now() WHERE id = $1",
            account_id,
        )


def _body(amount_cents: int = 12500, **overrides) -> dict:
    body = {
        "transaction_id": str(uuid.uuid4()),
        "amount_cents": amount_cents,
        "date": PAST_DATE,
    }
    body.update(overrides)
    return body


async def _post_opening(client, account_id: str, body: dict, idem_key: Optional[str] = None):
    return await client.post(
        f"/v1/accounts/{account_id}/opening-balance",
        json=body,
        headers={"X-Idempotency-Key": idem_key or str(uuid.uuid4())},
    )


@pytest.mark.asyncio
async def test_opening_balance_happy_path(client, test_data):
    """201: seed transaction created under @Opening, balance updated."""
    account_id = await _make_account(test_data.user_id)
    try:
        r = await _post_opening(client, account_id, _body(12500, title="SALDO INICIAL"))
        assert r.status_code == 201, r.text
        tx = r.json()
        assert tx["transaction_type"] == 2  # positive -> income type
        assert tx["amount_cents"] == 12500  # stored positive
        assert tx["title"] == "SALDO INICIAL"
        assert tx["account_id"] == account_id

        # The auto-created category is the @Opening system row.
        r = await client.get("/v1/categories")
        cats = {c["id"]: c for c in r.json()["items"]}
        opening = cats[tx["category_id"]]
        assert opening["is_system"] is True

        # Balance moved by exactly the seed amount.
        r = await client.get(f"/v1/accounts/{account_id}")
        assert r.json()["current_balance_cents"] == 12500
    finally:
        await _soft_delete_account(account_id)


@pytest.mark.asyncio
async def test_negative_opening_balance(client, test_data):
    """Negative seed (credit card starting in debt) -> expense type, balance negative."""
    account_id = await _make_account(test_data.user_id)
    try:
        r = await _post_opening(client, account_id, _body(-30000))
        assert r.status_code == 201, r.text
        tx = r.json()
        assert tx["transaction_type"] == 1  # negative -> expense type
        assert tx["amount_cents"] == 30000  # stored positive

        r = await client.get(f"/v1/accounts/{account_id}")
        assert r.json()["current_balance_cents"] == -30000
    finally:
        await _soft_delete_account(account_id)


@pytest.mark.asyncio
async def test_default_title(client, test_data):
    account_id = await _make_account(test_data.user_id)
    try:
        r = await _post_opening(client, account_id, _body())
        assert r.status_code == 201, r.text
        assert r.json()["title"] == "Opening balance"
    finally:
        await _soft_delete_account(account_id)


@pytest.mark.asyncio
async def test_second_opening_balance_conflicts(client, test_data):
    """One active opening balance per account — second POST is 409."""
    account_id = await _make_account(test_data.user_id)
    try:
        r = await _post_opening(client, account_id, _body(1000))
        assert r.status_code == 201, r.text
        r = await _post_opening(client, account_id, _body(2000))
        assert r.status_code == 409, r.text
    finally:
        await _soft_delete_account(account_id)


@pytest.mark.asyncio
async def test_duplicate_transaction_id_conflicts(client, test_data):
    """The same client-supplied transaction_id cannot be reused (re-run dedup)."""
    account_a = await _make_account(test_data.user_id)
    account_b = await _make_account(test_data.user_id)
    try:
        body = _body(1000)
        r = await _post_opening(client, account_a, body)
        assert r.status_code == 201, r.text
        # Same transaction_id against a different account: id collision, not
        # the one-per-account guard.
        r = await _post_opening(client, account_b, body)
        assert r.status_code == 409, r.text
    finally:
        await _soft_delete_account(account_a)
        await _soft_delete_account(account_b)


@pytest.mark.asyncio
async def test_validation_errors(client, test_data):
    account_id = await _make_account(test_data.user_id)
    person_id = await _make_person_account(test_data.user_id)
    try:
        # Zero amount
        r = await _post_opening(client, account_id, _body(0))
        assert r.status_code == 422, r.text
        assert "amount_cents" in r.json()["error"]["fields"]

        # Future date
        r = await _post_opening(client, account_id, _body(date="2030-01-01T00:00:00Z"))
        assert r.status_code == 422, r.text
        assert "date" in r.json()["error"]["fields"]

        # Person account
        r = await _post_opening(client, person_id, _body())
        assert r.status_code == 422, r.text
        assert "account_id" in r.json()["error"]["fields"]

        # Unknown account
        r = await _post_opening(client, str(uuid.uuid4()), _body())
        assert r.status_code == 422, r.text
        assert "account_id" in r.json()["error"]["fields"]

        # Unknown body field is rejected (extra="forbid")
        r = await _post_opening(client, account_id, _body(bogus_field=1))
        assert r.status_code == 422, r.text
    finally:
        await _soft_delete_account(account_id)
        await _soft_delete_account(person_id)


@pytest.mark.asyncio
async def test_idempotency_replay(client, test_data):
    """Same X-Idempotency-Key replays the stored response; balance applied once."""
    account_id = await _make_account(test_data.user_id)
    try:
        idem_key = str(uuid.uuid4())
        body = _body(7000)
        r1 = await _post_opening(client, account_id, body, idem_key=idem_key)
        r2 = await _post_opening(client, account_id, body, idem_key=idem_key)
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, r2.text
        assert r1.json()["id"] == r2.json()["id"]

        r = await client.get(f"/v1/accounts/{account_id}")
        assert r.json()["current_balance_cents"] == 7000  # not 14000
    finally:
        await _soft_delete_account(account_id)


@pytest.mark.asyncio
async def test_reports_exclude_opening_balance(client, test_data):
    """The seed month's report shows no @Opening row and totals ignore the seed.

    Uses 2025-11 — a month no other test writes to — so the totals
    assertion can be exact.
    """
    account_id = await _make_account(test_data.user_id)
    try:
        r = await _post_opening(
            client, account_id, _body(555500, date="2025-11-03T12:00:00Z")
        )
        assert r.status_code == 201, r.text
        opening_category_id = r.json()["category_id"]

        # A normal income in the same month, so inflow has a known value.
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"real-income-{uuid.uuid4()}",
                "amount_cents": 7777,
                "date": "2025-11-05T12:00:00Z",
                "account_id": account_id,
                "category_id": test_data.category_id,
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text

        r = await client.get("/v1/reports/monthly", params={"year": 2025, "month": 11})
        assert r.status_code == 200, r.text
        report = r.json()

        category_ids = {c["id"] for c in report["categories"]}
        assert opening_category_id not in category_ids

        # The account is PEN, which is the home currency, so the home figure is
        # the native amount unchanged and no rate lookup is involved. Reports
        # carry home values only — a native cross-account total would be summing
        # currencies that do not add up.
        totals = report["totals"]
        assert totals["inflow_home_cents"] == 7777  # seed's 555500 absent
        assert totals["net_home_cents"] == 7777
        assert totals["unconverted_count"] == 0

        # Rename tolerance: exclusion keys off system_key, not the name.
        r = await client.put(
            f"/v1/categories/{opening_category_id}",
            json={"name": f"Saldo Inicial {uuid.uuid4().hex[:6]}"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text

        r = await client.get("/v1/reports/monthly", params={"year": 2025, "month": 11})
        report = r.json()
        assert opening_category_id not in {c["id"] for c in report["categories"]}
        assert report["totals"]["inflow_home_cents"] == 7777

        # The one-per-account guard also survives the rename.
        r = await _post_opening(client, account_id, _body(1000))
        assert r.status_code == 409, r.text
    finally:
        await _soft_delete_account(account_id)
