"""Colour over the wire — omitted defaults, junk is refused (bug account-color).

The sibling of ``test_sort_order_append.py``, and deliberately the same shape:
that file pins ``or 0`` → ``is not None`` for ``sort_order`` after an explicit 0
was being eaten by truthiness. ``color`` had the identical collapse two lines
away in the same INSERT and survived that fix — an explicitly-sent ``""`` became
the default blue instead of being honoured or refused.

Owner decision 2026-08-13 went further than the reported bug: reject anything
that is not a 6-digit hex value, not just the empty string. Fixing only ``""``
would have left ``banana`` stored happily one step over, which is the pattern
CLAUDE.md calls a smell — and on categories, whose ``color`` is *required*, junk
was being stored rather than defaulted, which is the worse half nobody had
reported.

Both resources are covered because they fail differently: accounts have an
optional colour with a default, categories a required one with none.

Run: .venv/bin/pytest tests/test_account_color_rules.py -v
"""
import uuid

import pytest

from app.constants import DEFAULT_ACCOUNT_COLOR

# One representative of each rejection class; the full matrix lives in
# tests/test_sql031_color_checks.py against the validator directly.
BAD_COLORS = ["", "   ", "banana", "#fff", "#3b82f6 "]


def _idem():
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _account_body(**overrides):
    body = {
        "id": str(uuid.uuid4()),
        "name": f"Colour {uuid.uuid4().hex[:8]}",
        "currency_code": "PEN",
    }
    body.update(overrides)
    return body


def _category_body(**overrides):
    body = {
        "id": str(uuid.uuid4()),
        "name": f"Colour {uuid.uuid4().hex[:8]}",
        "color": "#112233",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Accounts — optional colour, with a default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_omitted_color_falls_to_the_default(client, test_data):
    """The behaviour the fix had to preserve. `is not None` keeps this working;
    dropping the fallback entirely would have been the obvious wrong fix."""
    response = await client.post("/v1/accounts", json=_account_body(), headers=_idem())
    assert response.status_code == 201, response.text
    assert response.json()["color"] == DEFAULT_ACCOUNT_COLOR


@pytest.mark.asyncio
async def test_an_explicit_color_is_stored_verbatim(client, test_data):
    """Including case — the engine does not lowercase what it accepted."""
    response = await client.post(
        "/v1/accounts", json=_account_body(color="#00AA00"), headers=_idem()
    )
    assert response.status_code == 201, response.text
    assert response.json()["color"] == "#00AA00"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_COLORS, ids=[c or "(empty)" for c in BAD_COLORS])
async def test_create_rejects_junk_instead_of_defaulting(client, test_data, bad):
    """The bug itself: `""` used to come back as #3b82f6, with a 201."""
    response = await client.post(
        "/v1/accounts", json=_account_body(color=bad), headers=_idem()
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "color" in error["fields"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_COLORS, ids=[c or "(empty)" for c in BAD_COLORS])
async def test_update_rejects_junk(client, test_data, bad):
    """The update path reached `dynamic_update` unvalidated, so `""` was stored
    verbatim there while create silently defaulted it — the two disagreed."""
    created = await client.post("/v1/accounts", json=_account_body(), headers=_idem())
    account_id = created.json()["id"]

    response = await client.put(
        f"/v1/accounts/{account_id}", json={"color": bad}, headers=_idem()
    )
    assert response.status_code == 422, response.text
    assert "color" in response.json()["error"]["fields"]

    unchanged = await client.get(f"/v1/accounts/{account_id}")
    assert unchanged.json()["color"] == DEFAULT_ACCOUNT_COLOR, "a refused update stores nothing"


@pytest.mark.asyncio
async def test_update_to_a_valid_color_still_works(client, test_data):
    """Paired with the rejections so the guard is pinned as selective."""
    created = await client.post("/v1/accounts", json=_account_body(), headers=_idem())
    account_id = created.json()["id"]

    response = await client.put(
        f"/v1/accounts/{account_id}", json={"color": "#abc123"}, headers=_idem()
    )
    assert response.status_code == 200, response.text
    assert response.json()["color"] == "#abc123"


# ---------------------------------------------------------------------------
# Categories — required colour, no default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_COLORS, ids=[c or "(empty)" for c in BAD_COLORS])
async def test_category_create_rejects_junk(client, test_data, bad):
    """The worse half of the bug, and the one nobody had reported.

    `CategoryCreateRequest.color` is required and was bound verbatim, so this
    stored the junk rather than defaulting it — no truthiness collapse to notice,
    just a bad value on the row.
    """
    response = await client.post(
        "/v1/categories", json=_category_body(color=bad), headers=_idem()
    )
    assert response.status_code == 422, response.text
    assert "color" in response.json()["error"]["fields"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_COLORS, ids=[c or "(empty)" for c in BAD_COLORS])
async def test_category_update_rejects_junk(client, test_data, bad):
    created = await client.post("/v1/categories", json=_category_body(), headers=_idem())
    category_id = created.json()["id"]

    response = await client.put(
        f"/v1/categories/{category_id}", json={"color": bad}, headers=_idem()
    )
    assert response.status_code == 422, response.text
    assert "color" in response.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_category_valid_color_round_trips(client, test_data):
    created = await client.post(
        "/v1/categories", json=_category_body(color="#ABCDEF"), headers=_idem()
    )
    assert created.status_code == 201, created.text
    assert created.json()["color"] == "#ABCDEF"

    updated = await client.put(
        f"/v1/categories/{created.json()['id']}",
        json={"color": "#010203"}, headers=_idem(),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["color"] == "#010203"


# ---------------------------------------------------------------------------
# The neighbour this fix must not disturb
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hashtags_are_untouched(client, test_data):
    """Hashtags route through `update_named_resource`, where the colour guard
    now lives — but they have no colour column and no `color` field on their
    update schema, so the guard can never fire for them. Pinned because a guard
    added to a shared helper is exactly the kind of change that catches an
    unrelated resource."""
    created = await client.post(
        "/v1/hashtags",
        json={"id": str(uuid.uuid4()), "name": f"tag{uuid.uuid4().hex[:8]}"},
        headers=_idem(),
    )
    assert created.status_code == 201, created.text

    renamed = await client.put(
        f"/v1/hashtags/{created.json()['id']}",
        json={"name": f"tag{uuid.uuid4().hex[:8]}"}, headers=_idem(),
    )
    assert renamed.status_code == 200, renamed.text
