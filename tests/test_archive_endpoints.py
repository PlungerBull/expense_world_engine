"""Integration tests for the archive/unarchive surface area.

Covers the engine work shipped alongside sql/014:
  * `POST /accounts/{id}/unarchive` (parity with the existing /archive route).
  * `POST /categories/{id}/archive` + `/unarchive` (new), including the
    system-category 403 guard.
  * `POST /hashtags/{id}/archive` + `/unarchive` (new), including the
    deliberate non-cascade to junction rows.
  * `?include_archived=true` on `GET /categories` and `GET /hashtags`.
  * `is_archived` field present in `GET /sync` payloads for both resources.
  * `?include_archived=true` on `GET /dashboard` populating the three
    archived panels with lifetime totals; default response leaves them null.
  * Attach guard: archived categories/hashtags are rejected by every
    transaction-attach surface (POST /transactions, PUT /transactions,
    POST /transactions/batch, POST /inbox/{id}/promote) so archive means
    "retired" engine-wide, not just "hidden in the iOS picker".

Run: .venv/bin/pytest tests/test_archive_endpoints.py -v
"""
import uuid

import pytest

from app import db


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


async def _cleanup_account(account_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            account_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
            account_id, user_id,
        )


async def _cleanup_category(category_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            category_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = $1 AND user_id = $2",
            category_id, user_id,
        )


async def _cleanup_hashtag(hashtag_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE hashtag_id = $1 AND user_id = $2",
            hashtag_id, user_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            hashtag_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_hashtags WHERE id = $1 AND user_id = $2",
            hashtag_id, user_id,
        )


async def _activity_actions(resource_id: str, user_id: str) -> list[int]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action FROM activity_log
            WHERE resource_id = $1 AND user_id = $2
            ORDER BY created_at ASC
            """,
            resource_id, user_id,
        )
    return [r["action"] for r in rows]


# ---------------------------------------------------------------------------
# Account unarchive (parity with existing archive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_archive_unarchive_round_trip(client, test_data):
    """create → archive → unarchive: is_archived flips both ways and the
    activity log shows two UPDATED entries on top of CREATED."""
    account_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/accounts",
        json={
            "id": account_id,
            "name": f"archive-acct-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        archive_r = await client.post(
            f"/v1/accounts/{account_id}/archive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert archive_r.status_code == 200, archive_r.text
        assert archive_r.json()["is_archived"] is True

        unarchive_r = await client.post(
            f"/v1/accounts/{account_id}/unarchive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert unarchive_r.status_code == 200, unarchive_r.text
        body = unarchive_r.json()
        assert body["is_archived"] is False
        # CREATED + 2 UPDATED (archive + unarchive) = 3 mutations
        assert body["version"] >= 3

        # Activity actions: 1 (CREATED), 2 (UPDATED), 2 (UPDATED).
        assert await _activity_actions(account_id, test_data.user_id) == [1, 2, 2]
    finally:
        await _cleanup_account(account_id, test_data.user_id)


@pytest.mark.asyncio
async def test_account_unarchive_404_on_missing(client):
    r = await client.post(
        f"/v1/accounts/{uuid.uuid4()}/unarchive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Category archive / unarchive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_archive_round_trip_and_list_filter(client, test_data):
    """Archived categories drop from the default list and reappear with
    `?include_archived=true`. Unarchive restores them to the default list.
    """
    category_id = str(uuid.uuid4())
    name = f"archive-cat-{uuid.uuid4()}"
    create_r = await client.post(
        "/v1/categories",
        json={"id": category_id, "name": name, "color": "#abc123"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        archive_r = await client.post(
            f"/v1/categories/{category_id}/archive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert archive_r.status_code == 200, archive_r.text
        assert archive_r.json()["is_archived"] is True

        # Default list excludes archived rows.
        default_list = await client.get("/v1/categories?limit=200")
        assert default_list.status_code == 200
        ids_default = {c["id"] for c in default_list.json()["items"]}
        assert category_id not in ids_default

        # `include_archived=true` brings them back.
        archived_list = await client.get("/v1/categories?include_archived=true&limit=200")
        assert archived_list.status_code == 200
        archived_row = next(
            (c for c in archived_list.json()["items"] if c["id"] == category_id),
            None,
        )
        assert archived_row is not None
        assert archived_row["is_archived"] is True

        # Unarchive flips the flag back and the row reappears in the default list.
        unarchive_r = await client.post(
            f"/v1/categories/{category_id}/unarchive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert unarchive_r.status_code == 200, unarchive_r.text
        assert unarchive_r.json()["is_archived"] is False

        after_list = await client.get("/v1/categories?limit=200")
        ids_after = {c["id"] for c in after_list.json()["items"]}
        assert category_id in ids_after

        assert await _activity_actions(category_id, test_data.user_id) == [1, 2, 2]
    finally:
        await _cleanup_category(category_id, test_data.user_id)


@pytest.mark.asyncio
async def test_category_archive_rejects_system_category(client, test_data):
    """System categories (is_system=true) cannot be archived — same guard
    as DELETE. The transfer pipeline relies on @Transfer / @Debt being
    available regardless of UI state.
    """
    sys_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, is_system, system_key, sort_order, created_at, updated_at)
               VALUES ($1, $2, '@TestSystem', '#000000', true, NULL, 0, now(), now())""",
            sys_id, test_data.user_id,
        )
    try:
        r = await client.post(
            f"/v1/categories/{sys_id}/archive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 403, r.text
    finally:
        await _cleanup_category(sys_id, test_data.user_id)


@pytest.mark.asyncio
async def test_category_archive_404_on_missing(client):
    r = await client.post(
        f"/v1/categories/{uuid.uuid4()}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Hashtag archive / unarchive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hashtag_archive_round_trip_and_list_filter(client, test_data):
    """Archived hashtags drop from the default list and reappear with
    `?include_archived=true`. Junction rows are NOT touched.
    """
    hashtag_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/hashtags",
        json={"id": hashtag_id, "name": f"archive-tag-{uuid.uuid4()}"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    # Tag a transaction so we can verify the junction row survives archive.
    txn_id = str(uuid.uuid4())
    create_txn_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"tagged-archive-{uuid.uuid4()}",
            "amount_cents": -100,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [hashtag_id],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn_r.status_code == 201, create_txn_r.text

    try:
        archive_r = await client.post(
            f"/v1/hashtags/{hashtag_id}/archive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert archive_r.status_code == 200, archive_r.text
        assert archive_r.json()["is_archived"] is True

        # Junction row must remain active — archive is hide-only, not destroy.
        async with db.pool.acquire() as conn:
            active_junctions = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transaction_hashtags
                WHERE hashtag_id = $1 AND deleted_at IS NULL
                """,
                hashtag_id,
            )
        assert active_junctions == 1, (
            f"Hashtag archive must not cascade to junctions; "
            f"found {active_junctions} active rows (expected 1)"
        )

        default_list = await client.get("/v1/hashtags?limit=200")
        ids_default = {h["id"] for h in default_list.json()["items"]}
        assert hashtag_id not in ids_default

        archived_list = await client.get("/v1/hashtags?include_archived=true&limit=200")
        archived_row = next(
            (h for h in archived_list.json()["items"] if h["id"] == hashtag_id),
            None,
        )
        assert archived_row is not None
        assert archived_row["is_archived"] is True

        unarchive_r = await client.post(
            f"/v1/hashtags/{hashtag_id}/unarchive",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert unarchive_r.status_code == 200, unarchive_r.text
        assert unarchive_r.json()["is_archived"] is False

        assert await _activity_actions(hashtag_id, test_data.user_id) == [1, 2, 2]
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            # Restore the test_data account balance (we created a -100 expense).
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 100 WHERE id = $1",
                test_data.account_id,
            )
        await _cleanup_hashtag(hashtag_id, test_data.user_id)


@pytest.mark.asyncio
async def test_hashtag_archive_404_on_missing(client):
    r = await client.post(
        f"/v1/hashtags/{uuid.uuid4()}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Sync exposes is_archived for categories and hashtags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_payload_includes_is_archived_field(client, test_data):
    """Every category and hashtag row in `/sync` carries `is_archived`.

    The schema-level addition flows through `category_from_row` /
    `hashtag_from_row`, which sync uses verbatim. Pin the field's presence
    so a future schema rename or accidental field drop is caught.
    """
    headers = {"X-Client-Id": str(uuid.uuid4())}
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=headers)
    assert r.status_code == 200
    body = r.json()

    assert body["categories"], "sync wildcard should return at least one category"
    for c in body["categories"]:
        assert "is_archived" in c, f"category row missing is_archived: {c}"
        assert isinstance(c["is_archived"], bool)

    assert body["hashtags"], "sync wildcard should return at least one hashtag"
    for h in body["hashtags"]:
        assert "is_archived" in h, f"hashtag row missing is_archived: {h}"
        assert isinstance(h["is_archived"], bool)


# ---------------------------------------------------------------------------
# Dashboard `?include_archived=true` panels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_default_omits_archived_panels(client):
    """Without the flag, the three archived fields are present and null."""
    r = await client.get("/v1/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archived_accounts"] is None
    assert body["archived_categories"] is None
    assert body["archived_hashtags"] is None


@pytest.mark.asyncio
async def test_dashboard_include_archived_returns_lifetime_totals(client, test_data):
    """Flag flips the three panels on. Archived category lifetime total
    matches the signed sum of every transaction ever attributed to it.
    """
    # Create a category, attach a -300 expense, then archive it.
    cat_id = str(uuid.uuid4())
    cat_name = f"lifetime-cat-{uuid.uuid4()}"
    create_cat = await client.post(
        "/v1/categories",
        json={"id": cat_id, "name": cat_name, "color": "#112233"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_cat.status_code == 201

    txn_id = str(uuid.uuid4())
    create_txn = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"lifetime-tx-{uuid.uuid4()}",
            "amount_cents": -300,
            "date": "2026-01-15T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": cat_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn.status_code == 201, create_txn.text

    archive_r = await client.post(
        f"/v1/categories/{cat_id}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert archive_r.status_code == 200

    try:
        r = await client.get("/v1/dashboard?include_archived=true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["archived_accounts"] is not None
        assert isinstance(body["archived_accounts"], list)
        assert body["archived_categories"] is not None
        assert body["archived_hashtags"] is not None

        archived_cat = next(
            (c for c in body["archived_categories"] if c["id"] == cat_id),
            None,
        )
        assert archived_cat is not None, (
            f"newly-archived category {cat_id} not surfaced in archived_categories"
        )
        # Expense of -300 → signed lifetime_spent_cents is -300.
        assert archived_cat["lifetime_spent_cents"] == -300
        assert archived_cat["lifetime_spent_home_cents"] == -300
        assert archived_cat["name"] == cat_name
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            # Restore the test_data account balance (-300 expense).
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 300 WHERE id = $1",
                test_data.account_id,
            )
        await _cleanup_category(cat_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Attach guard: archived categories/hashtags are rejected by every
# transaction-attach surface (create, update, batch, promote).
# ---------------------------------------------------------------------------


async def _make_archived_category(client, idem) -> str:
    cat_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/categories",
        json={"id": cat_id, "name": f"guard-cat-{uuid.uuid4()}", "color": "#445566"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text
    archive_r = await client.post(
        f"/v1/categories/{cat_id}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert archive_r.status_code == 200, archive_r.text
    return cat_id


async def _make_archived_hashtag(client) -> str:
    h_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/hashtags",
        json={"id": h_id, "name": f"guard-tag-{uuid.uuid4()}"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text
    archive_r = await client.post(
        f"/v1/hashtags/{h_id}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert archive_r.status_code == 200, archive_r.text
    return h_id


@pytest.mark.asyncio
async def test_create_transaction_rejects_archived_category(client, test_data):
    cat_id = await _make_archived_category(client, idem=None)
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"guard-create-{uuid.uuid4()}",
                "amount_cents": -100,
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": cat_id,
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        body = r.json()["error"]
        assert body["code"] == "VALIDATION_ERROR"
        assert "category_id" in (body.get("fields") or {})
        assert "non-archived" in body["fields"]["category_id"]
    finally:
        await _cleanup_category(cat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_update_transaction_rejects_archived_category(client, test_data):
    """An existing transaction cannot be re-pointed at an archived
    category via PUT — same guard as create.
    """
    txn_id = str(uuid.uuid4())
    create_txn = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"guard-update-{uuid.uuid4()}",
            "amount_cents": -200,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn.status_code == 201, create_txn.text

    cat_id = await _make_archived_category(client, idem=None)

    try:
        r = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"category_id": cat_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        assert "category_id" in (r.json()["error"].get("fields") or {})
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            # Restore the test_data account balance (-200 expense).
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + 200 WHERE id = $1",
                test_data.account_id,
            )
        await _cleanup_category(cat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_create_transaction_rejects_archived_hashtag(client, test_data):
    h_id = await _make_archived_hashtag(client)
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"guard-tag-create-{uuid.uuid4()}",
                "amount_cents": -100,
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
                "hashtag_ids": [h_id],
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        assert "hashtag_ids" in (r.json()["error"].get("fields") or {})
    finally:
        await _cleanup_hashtag(h_id, test_data.user_id)


@pytest.mark.asyncio
async def test_batch_transactions_rejects_archived_category(client, test_data):
    cat_id = await _make_archived_category(client, idem=None)
    try:
        r = await client.post(
            "/v1/transactions/batch",
            json={
                "transactions": [
                    {
                        "id": str(uuid.uuid4()),
                        "title": f"guard-batch-{uuid.uuid4()}",
                        "amount_cents": -100,
                        "date": "2026-04-12T12:00:00Z",
                        "account_id": test_data.account_id,
                        "category_id": cat_id,
                    }
                ]
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        body = r.json()["error"]
        assert body["code"] == "VALIDATION_ERROR"
        # Batch errors carry per-item details — confirm category_id is the
        # field flagged on item 0.
        items = body.get("fields", {}).get("items") or []
        assert items, f"expected per-item failures, got: {body}"
        assert any(
            "category_id" in (item.get("fields") or {})
            for item in items
        ), body
    finally:
        await _cleanup_category(cat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_inbox_promote_rejects_archived_category(client, test_data):
    """Promote-time guard: an inbox row pointing at an archived category
    cannot promote, mirroring the create-transaction guard.
    """
    cat_id = await _make_archived_category(client, idem=None)
    inbox_id = str(uuid.uuid4())
    # The inbox endpoint accepts any category_id at create time (intentional —
    # inbox is sparse, validation happens at promote). We bypass the public
    # API and write the inbox row directly so we can target an already-archived
    # category without racing the archive call.
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_transaction_inbox
                (id, user_id, title, amount_cents, transaction_type, date,
                 account_id, category_id, exchange_rate, status,
                 created_at, updated_at)
            VALUES ($1, $2, 'guard-promote', 100, 1, now(),
                    $3, $4, 1.0, 1, now(), now())
            """,
            inbox_id, test_data.user_id, test_data.account_id, cat_id,
        )

    try:
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4()), "transfer_id": None},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        body = r.json()["error"]
        assert "category_id" in (body.get("fields") or {})
        assert "non-archived" in body["fields"]["category_id"]
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )
        await _cleanup_category(cat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_inbox_ready_filter_excludes_archived_category(client, test_data):
    """`GET /inbox?ready=true` must not surface inbox items pointing at
    archived categories — they aren't actually promotable, so listing
    them as ready would mislead the client.
    """
    cat_id = await _make_archived_category(client, idem=None)
    inbox_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_transaction_inbox
                (id, user_id, title, amount_cents, transaction_type, date,
                 account_id, category_id, exchange_rate, status,
                 created_at, updated_at)
            VALUES ($1, $2, 'guard-ready', 100, 1, now(),
                    $3, $4, 1.0, 1, now(), now())
            """,
            inbox_id, test_data.user_id, test_data.account_id, cat_id,
        )

    try:
        r = await client.get("/v1/inbox?ready=true&limit=200")
        assert r.status_code == 200, r.text
        ids = {item["id"] for item in r.json()["items"]}
        assert inbox_id not in ids, (
            "inbox row pointing at an archived category leaked into "
            "the ready=true list"
        )
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )
        await _cleanup_category(cat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_transaction_restore_rejects_archived_category(client, test_data):
    """If a category is archived after a transaction is soft-deleted,
    restoring the transaction must 422 — restore prerequisites check
    is_archived = false on referenced category, mirroring the existing
    account check.
    """
    cat_id = str(uuid.uuid4())
    create_cat = await client.post(
        "/v1/categories",
        json={"id": cat_id, "name": f"restore-guard-{uuid.uuid4()}", "color": "#778899"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_cat.status_code == 201

    txn_id = str(uuid.uuid4())
    create_txn = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"restore-guard-tx-{uuid.uuid4()}",
            "amount_cents": -150,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": cat_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn.status_code == 201, create_txn.text

    # Soft-delete txn (clears its reference from the category's active set),
    # then archive the category.
    del_r = await client.delete(
        f"/v1/transactions/{txn_id}",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert del_r.status_code == 200, del_r.text

    archive_r = await client.post(
        f"/v1/categories/{cat_id}/archive",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert archive_r.status_code == 200, archive_r.text

    try:
        restore_r = await client.post(
            f"/v1/transactions/{txn_id}/restore",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert restore_r.status_code == 422, restore_r.text
        body = restore_r.json()["error"]
        assert "category_id" in (body.get("fields") or {})
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
        await _cleanup_category(cat_id, test_data.user_id)
