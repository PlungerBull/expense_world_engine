"""Inbox titles are normalized on write (bug inbox-title).

A whitespace-only title used to be stored verbatim. Stored that way it was
*truthy* and `IS NOT NULL`, so it slipped past both readiness definitions —
`?ready=true`'s SQL and promote's own check — and landed in the ledger as a
blank-looking row, bypassing the trim-and-reject rule every direct ledger write
applies via `clean_name` / `normalize_name`.

Owner decision 2026-08-13: whitespace is an **unfilled field, not a value**.
`clean_name` maps it to NULL and the draft stays not-ready until a real title is
typed. Nothing is rejected — a half-finished draft is exactly what the inbox is
for. CLAUDE.md's inbox carve-out sanctions this and bounds it: the inbox is
"looser about *which fields are null*, never about how a field encodes its
meaning", and whitespace→NULL changes nullness only.

**No guard needed a code change**, which is the part worth stating. `?ready=true`
already had `i.title IS NOT NULL` and promote already had `if not
inbox_row["title"]`; both were written correctly and were simply being fed a
truthy value. So these tests pin behavior that emerges from one normalization,
and would regress the moment the normalization moves or is dropped.

Both readiness definitions are asserted together on every case — the two have
drifted before (WP7.2/7.3) and both source sites carry "keep these in step"
comments. A listed row must promote; an unlisted row must not.

Run: .venv/bin/pytest tests/test_inbox_title_normalization.py -v
"""
import uuid

import pytest

from app import db

PAST_DATE = "2020-06-15T12:00:00+00:00"


def _idem():
    return {"X-Idempotency-Key": str(uuid.uuid4())}


async def _draft(client, test_data, title):
    """Create an inbox draft that is ready in every respect except its title."""
    body = {
        "id": str(uuid.uuid4()),
        "amount_cents": -900,
        "date": PAST_DATE,
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    if title is not ...:
        body["title"] = title
    response = await client.post("/v1/inbox", json=body, headers=_idem())
    assert response.status_code == 201, response.text
    return response.json()


async def _stored_title(inbox_id):
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT title FROM expense_transaction_inbox WHERE id = $1", inbox_id
        )


async def _is_listed_ready(client, inbox_id):
    response = await client.get("/v1/inbox", params={"ready": "true"})
    assert response.status_code == 200, response.text
    return inbox_id in {row["id"] for row in response.json()["items"]}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

# Every shape that means "nothing was typed here". Tabs and newlines included
# because `.strip()` covers them and a regex-based fix might not.
BLANK_TITLES = ["   ", "\t", "\n  \t ", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize("title", BLANK_TITLES, ids=repr)
async def test_a_blank_title_is_stored_as_null(client, test_data, title):
    item = await _draft(client, test_data, title)

    assert item["title"] is None, "response must report the field as unfilled"
    assert await _stored_title(item["id"]) is None, "and it must be NULL in the row"


@pytest.mark.asyncio
async def test_a_blank_title_draft_is_not_ready_and_cannot_promote(client, test_data):
    """The two readiness definitions, asserted together.

    This is the whole point of the fix: the draft is still perfectly saveable,
    it just cannot graduate into the ledger while its title is unfilled.
    """
    item = await _draft(client, test_data, "   ")

    assert not await _is_listed_ready(client, item["id"])

    response = await client.post(
        f"/v1/inbox/{item['id']}/promote",
        json={"id": str(uuid.uuid4())},
        headers=_idem(),
    )
    assert response.status_code == 422, response.text
    assert "title" in response.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_a_padded_title_is_trimmed_not_rejected(client, test_data):
    """Surrounding whitespace on a real title is content, trimmed like the ledger
    trims it — the draft stays ready and promotes."""
    item = await _draft(client, test_data, "  Coffee  ")
    assert item["title"] == "Coffee"

    assert await _is_listed_ready(client, item["id"])
    response = await client.post(
        f"/v1/inbox/{item['id']}/promote",
        json={"id": str(uuid.uuid4())},
        headers=_idem(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Coffee", "the ledger row carries the trimmed title"


@pytest.mark.asyncio
async def test_an_omitted_title_is_unchanged(client, test_data):
    """Omitting the field was always legal and always meant NULL. The fix must
    not make a blank title behave differently from never supplying one."""
    item = await _draft(client, test_data, ...)
    assert item["title"] is None
    assert not await _is_listed_ready(client, item["id"])


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_updating_a_title_to_blank_clears_it_and_unreadies_the_draft(
    client, test_data
):
    """A ready draft can be walked back to not-ready by blanking its title.

    The update path had the same hole as create; without the fix this row keeps
    a truthy title and stays promotable.
    """
    item = await _draft(client, test_data, "Lunch")
    assert await _is_listed_ready(client, item["id"])

    response = await client.put(
        f"/v1/inbox/{item['id']}", json={"title": "   "}, headers=_idem()
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] is None

    assert not await _is_listed_ready(client, item["id"])


@pytest.mark.asyncio
async def test_updating_a_title_trims_it(client, test_data):
    item = await _draft(client, test_data, "Lunch")
    response = await client.put(
        f"/v1/inbox/{item['id']}", json={"title": "  Dinner  "}, headers=_idem()
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Dinner"


@pytest.mark.asyncio
async def test_an_explicit_null_title_is_still_rejected(client, test_data):
    """The deliberate asymmetry, pinned so it reads as designed.

    `{"title": "   "}` stores NULL; `{"title": null}` is 422. They differ because
    they are different kinds of statement: whitespace is a field you left blank,
    while an explicit null is a *clear* operation, and `extract_update_fields`
    refuses null across every endpoint (null is not a verb — the rule exists so
    an immutable field cannot be nulled to dodge its guard). Documented in
    engine-spec; also pinned by test_bug_10_2_shapes.py::
    test_inbox_put_rejects_explicit_null_title.
    """
    item = await _draft(client, test_data, "Lunch")
    response = await client.put(
        f"/v1/inbox/{item['id']}", json={"title": None}, headers=_idem()
    )
    assert response.status_code == 422, response.text
    assert "title" in response.json()["error"]["fields"]
    assert await _stored_title(item["id"]) == "Lunch", "a refused update changes nothing"


@pytest.mark.asyncio
async def test_description_is_not_normalized(client, test_data):
    """The scope boundary. The ledger stores `description` verbatim, so
    normalizing it here would make the draft stricter than the row it becomes —
    a new divergence, not a fix."""
    item = await _draft(client, test_data, "Lunch")
    response = await client.put(
        f"/v1/inbox/{item['id']}", json={"description": "  spaced  "}, headers=_idem()
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "  spaced  "
