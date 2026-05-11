"""Regression tests for hashtag_ids on every transaction-returning endpoint.

Per docs/engine-spec.md §Transactions and docs/api-design-principles.md §3a:
every read endpoint that returns a transaction (in any shape) must embed
``hashtag_ids: [uuid, ...]`` sorted ascending — ``[]`` when the transaction
has no attached hashtags, never ``null``, never omitted.

The covered surface:

  * GET  /v1/transactions/{id}
  * GET  /v1/transactions               (list)
  * POST /v1/transactions                (single create response)
  * PUT  /v1/transactions/{id}           (update response)
  * DELETE /v1/transactions/{id}         (delete response body)
  * POST /v1/transactions/{id}/restore   (restore response body)
  * POST /v1/transactions/batch          (each created item)
  * POST /v1/inbox/{id}/promote          (promoted transaction)
  * GET  /v1/reconciliations/{id}        (each embedded transaction)
  * GET  /v1/sync                        (the original embed site)

These tests own their fixtures (fresh hashtags + transactions per test) so
state from one parametrized case can't leak into another. Cleanup hard-
deletes the test rows + their activity_log entries — same pattern used by
test_audit_response_shape.py.
"""
from __future__ import annotations

import uuid
from typing import Iterable

import pytest

from app import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _idem() -> dict[str, str]:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _assert_hashtag_ids_shape(payload: dict, expected: Iterable[str]) -> None:
    """Single source of truth for the wire-shape contract.

    Verifies that ``hashtag_ids`` is present, is a list of strings, equals
    the expected set, and is sorted ascending. ``expected`` is treated as
    a set — the assertion ignores caller ordering but enforces that the
    response itself is sorted ASC.
    """
    assert "hashtag_ids" in payload, f"missing hashtag_ids: keys={list(payload)}"
    got = payload["hashtag_ids"]
    assert isinstance(got, list), f"hashtag_ids is {type(got).__name__}, expected list"
    assert all(isinstance(h, str) for h in got), "hashtag_ids must be a list of strings"
    assert sorted(got) == got, f"hashtag_ids must be sorted ascending; got {got}"
    assert set(got) == set(expected), f"hashtag_ids mismatch: got {got}, expected {sorted(expected)}"


async def _create_hashtag(client, name_prefix: str) -> str:
    h_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/hashtags",
        json={"id": h_id, "name": f"{name_prefix}-{uuid.uuid4()}"},
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    return h_id


async def _create_txn(client, test_data, hashtag_ids: list[str] | None = None) -> tuple[str, dict]:
    txn_id = str(uuid.uuid4())
    body: dict = {
        "id": txn_id,
        "title": f"wire-shape-{uuid.uuid4()}",
        "amount_cents": -250,
        "date": "2026-04-12T12:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    if hashtag_ids is not None:
        body["hashtag_ids"] = hashtag_ids
    r = await client.post("/v1/transactions", json=body, headers=_idem())
    assert r.status_code == 201, r.text
    return txn_id, r.json()


async def _cleanup_txn(txn_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
            txn_id, user_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            txn_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
            txn_id, user_id,
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


# ---------------------------------------------------------------------------
# Single-endpoint regression — parametrized over every endpoint shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hashtag_count", [0, 1, 2])
async def test_get_single_endpoint(client, test_data, hashtag_count):
    """GET /v1/transactions/{id} returns hashtag_ids in every case."""
    tags = [await _create_hashtag(client, "single") for _ in range(hashtag_count)]
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=tags or None)

    try:
        r = await client.get(f"/v1/transactions/{txn_id}")
        assert r.status_code == 200, r.text
        _assert_hashtag_ids_shape(r.json(), tags)
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        for h in tags:
            await _cleanup_hashtag(h, test_data.user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("hashtag_count", [0, 2])
async def test_list_endpoint(client, test_data, hashtag_count):
    """GET /v1/transactions (list) embeds hashtag_ids on every row."""
    tags = [await _create_hashtag(client, "list") for _ in range(hashtag_count)]
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=tags or None)

    try:
        r = await client.get("/v1/transactions", params={"limit": 200})
        assert r.status_code == 200, r.text
        rows = r.json()["items"]
        match = next((t for t in rows if t["id"] == txn_id), None)
        assert match is not None, f"created txn {txn_id} not in list response"
        _assert_hashtag_ids_shape(match, tags)
        # Every row in the list must carry the field — never omitted.
        for row in rows:
            assert "hashtag_ids" in row, f"list row {row['id']} missing hashtag_ids"
            assert isinstance(row["hashtag_ids"], list)
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        for h in tags:
            await _cleanup_hashtag(h, test_data.user_id)


@pytest.mark.asyncio
async def test_create_response_embeds_hashtag_ids(client, test_data):
    """POST /v1/transactions response body carries hashtag_ids."""
    tags = [await _create_hashtag(client, "create") for _ in range(2)]
    txn_id, body = await _create_txn(client, test_data, hashtag_ids=tags)

    try:
        _assert_hashtag_ids_shape(body, tags)
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        for h in tags:
            await _cleanup_hashtag(h, test_data.user_id)


@pytest.mark.asyncio
async def test_create_with_no_hashtags_returns_empty_array(client, test_data):
    """[] not null, not omitted, when no hashtags were attached."""
    txn_id, body = await _create_txn(client, test_data, hashtag_ids=None)
    try:
        _assert_hashtag_ids_shape(body, [])
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)


@pytest.mark.asyncio
async def test_put_response_reflects_new_hashtag_set(client, test_data):
    """PUT /v1/transactions/{id} response carries the post-PUT hashtag set —
    including overlap PUTs, empty-body fast paths, and clear-to-empty.

    The overlap case ([A] → [A, B]) is the natural shape of "add a
    hashtag to a transaction that already has one." Covered explicitly
    because ``_sync_hashtags`` previously crashed on it (UNIQUE conflict
    on the re-INSERT of an already-attached hashtag); now handled by an
    ``ON CONFLICT DO UPDATE`` upsert.
    """
    h_a = await _create_hashtag(client, "put-a")
    h_b = await _create_hashtag(client, "put-b")
    h_c = await _create_hashtag(client, "put-c")
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=[h_a])

    try:
        # Overlap: keep h_a, add h_b.
        r = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": [h_a, h_b]},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text
        _assert_hashtag_ids_shape(r.json(), [h_a, h_b])

        # Non-overlapping replacement: [h_a, h_b] → [h_c].
        r2 = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": [h_c]},
            headers=_idem(),
        )
        assert r2.status_code == 200, r2.text
        _assert_hashtag_ids_shape(r2.json(), [h_c])

        # PUT with empty body — fast path still embeds the field with
        # the current set.
        r3 = await client.put(
            f"/v1/transactions/{txn_id}",
            json={},
            headers=_idem(),
        )
        assert r3.status_code == 200, r3.text
        _assert_hashtag_ids_shape(r3.json(), [h_c])

        # PUT clearing hashtags returns [].
        r4 = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": []},
            headers=_idem(),
        )
        assert r4.status_code == 200, r4.text
        _assert_hashtag_ids_shape(r4.json(), [])
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        for h in (h_a, h_b, h_c):
            await _cleanup_hashtag(h, test_data.user_id)


@pytest.mark.asyncio
async def test_put_reattach_cycle_keeps_junction_id_stable(client, test_data):
    """Attach → detach → re-attach a hashtag and verify the junction row's
    UUID is stable across the cycle (one row per logical (txn, hashtag)
    pair, not N+1 rows accumulating per cycle).

    This is the second invariant of the ON CONFLICT DO UPDATE pattern:
    a hashtag's lifecycle on a transaction collapses to a single
    junction row that toggles ``deleted_at`` instead of producing new
    rows on each re-attach.
    """
    h = await _create_hashtag(client, "reattach")
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=[h])

    async def _junction_id() -> str | None:
        async with db.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT id FROM expense_transaction_hashtags
                WHERE transaction_id = $1 AND hashtag_id = $2
                  AND user_id = $3
                """,
                txn_id, h, test_data.user_id,
            )

    try:
        initial_id = await _junction_id()
        assert initial_id is not None

        # Detach
        r = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": []},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text
        _assert_hashtag_ids_shape(r.json(), [])

        # Re-attach
        r2 = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": [h]},
            headers=_idem(),
        )
        assert r2.status_code == 200, r2.text
        _assert_hashtag_ids_shape(r2.json(), [h])

        # Junction UUID survived the cycle.
        reattached_id = await _junction_id()
        assert reattached_id == initial_id, (
            f"junction id churned across re-attach: initial={initial_id} "
            f"reattached={reattached_id}"
        )

        # And the (txn, hashtag) pair still has exactly one row in the
        # table — no N+1 accumulation.
        async with db.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT count(*) FROM expense_transaction_hashtags
                WHERE transaction_id = $1 AND hashtag_id = $2 AND user_id = $3
                """,
                txn_id, h, test_data.user_id,
            )
        assert count == 1, f"expected 1 junction row, got {count}"
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        await _cleanup_hashtag(h, test_data.user_id)


@pytest.mark.asyncio
async def test_delete_and_restore_response_bodies(client, test_data):
    """DELETE returns an empty hashtag_ids (junctions cascade-soft-delete);
    POST /restore re-activates the junctions and the response reflects them.
    """
    tags = [await _create_hashtag(client, "delete") for _ in range(2)]
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=tags)

    try:
        delete_r = await client.delete(f"/v1/transactions/{txn_id}", headers=_idem())
        assert delete_r.status_code == 200, delete_r.text
        # After cascade-soft-delete the active hashtag set is empty.
        _assert_hashtag_ids_shape(delete_r.json(), [])

        restore_r = await client.post(
            f"/v1/transactions/{txn_id}/restore", headers=_idem(),
        )
        assert restore_r.status_code == 200, restore_r.text
        # Junctions are re-activated by matching the parent's prior deleted_at.
        _assert_hashtag_ids_shape(restore_r.json(), tags)
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        for h in tags:
            await _cleanup_hashtag(h, test_data.user_id)


@pytest.mark.asyncio
async def test_batch_response_embeds_hashtag_ids_per_item(client, test_data):
    """POST /v1/transactions/batch returns hashtag_ids on every created row."""
    h1 = await _create_hashtag(client, "batch-a")
    h2 = await _create_hashtag(client, "batch-b")
    items = [
        {
            "id": str(uuid.uuid4()),
            "title": f"batch-{i}-{uuid.uuid4()}",
            "amount_cents": -100 - i,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": tags,
        }
        for i, tags in enumerate([[h1], [h1, h2], []])
    ]

    r = await client.post(
        "/v1/transactions/batch",
        json={"transactions": items},
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    created = r.json()["created"]
    assert len(created) == 3

    try:
        # Each item carries its own expected hashtag set, sorted ASC.
        expected_by_id = {item["id"]: item["hashtag_ids"] for item in items}
        for row in created:
            _assert_hashtag_ids_shape(row, expected_by_id[row["id"]])
    finally:
        for item in items:
            await _cleanup_txn(item["id"], test_data.user_id)
        await _cleanup_hashtag(h1, test_data.user_id)
        await _cleanup_hashtag(h2, test_data.user_id)


@pytest.mark.asyncio
async def test_inbox_promote_response_embeds_hashtag_ids(client, test_data):
    """POST /v1/inbox/{id}/promote returns the new transaction with hashtag_ids.

    Inbox items don't carry hashtags themselves, so a freshly-promoted
    transaction has hashtag_ids=[]. The point of this test is to lock the
    shape — the field is present, list-typed, empty array.
    """
    inbox_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/inbox",
        json={
            "id": inbox_id,
            "title": f"promote-{uuid.uuid4()}",
            "amount_cents": -500,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text

    target_id = str(uuid.uuid4())
    try:
        promote_r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": target_id},
            headers=_idem(),
        )
        assert promote_r.status_code == 200, promote_r.text
        _assert_hashtag_ids_shape(promote_r.json(), [])
    finally:
        await _cleanup_txn(target_id, test_data.user_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2",
                inbox_id, test_data.user_id,
            )


@pytest.mark.asyncio
async def test_reconciliation_detail_embeds_hashtag_ids_per_transaction(
    client, test_data,
):
    """GET /v1/reconciliations/{id} returns embedded transactions with hashtag_ids."""
    tags = [await _create_hashtag(client, "recon") for _ in range(2)]
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=tags)

    # Create a draft reconciliation and assign the transaction to it.
    recon_id = str(uuid.uuid4())
    recon_r = await client.post(
        "/v1/reconciliations",
        json={
            "id": recon_id,
            "account_id": test_data.account_id,
            "name": f"recon-{uuid.uuid4()}",
        },
        headers=_idem(),
    )
    assert recon_r.status_code == 201, recon_r.text

    assign_r = await client.put(
        f"/v1/transactions/{txn_id}",
        json={"reconciliation_id": recon_id},
        headers=_idem(),
    )
    assert assign_r.status_code == 200, assign_r.text

    try:
        detail_r = await client.get(f"/v1/reconciliations/{recon_id}")
        assert detail_r.status_code == 200, detail_r.text
        body = detail_r.json()
        assert body["transactions_total"] == 1
        embedded = body["transactions"][0]
        _assert_hashtag_ids_shape(embedded, tags)
    finally:
        # Unassign first so the recon cleanup doesn't trip a FK guard.
        await client.put(
            f"/v1/transactions/{txn_id}",
            json={"reconciliation_id": None},
            headers=_idem(),
        )
        await _cleanup_txn(txn_id, test_data.user_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                recon_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_reconciliations WHERE id = $1 AND user_id = $2",
                recon_id, test_data.user_id,
            )
        for h in tags:
            await _cleanup_hashtag(h, test_data.user_id)


# ---------------------------------------------------------------------------
# Field-shape invariants (sorted, list-typed, no None) on /sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_hashtag_ids_sorted_ascending(client, test_data):
    """The /sync embed (the original surface) must also be sorted ASC and
    list-typed. Regression guard for the shared serialization path.
    """
    h1 = await _create_hashtag(client, "sync-a")
    h2 = await _create_hashtag(client, "sync-b")
    h3 = await _create_hashtag(client, "sync-c")
    txn_id, _ = await _create_txn(client, test_data, hashtag_ids=[h2, h3, h1])

    try:
        r = await client.get(
            "/v1/sync",
            params={"sync_token": "*"},
            headers={"X-Client-Id": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        tx = next(t for t in r.json()["transactions"] if t["id"] == txn_id)
        _assert_hashtag_ids_shape(tx, [h1, h2, h3])
    finally:
        await _cleanup_txn(txn_id, test_data.user_id)
        for h in (h1, h2, h3):
            await _cleanup_hashtag(h, test_data.user_id)
