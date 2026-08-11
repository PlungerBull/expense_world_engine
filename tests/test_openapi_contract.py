"""Bug 10.1 — the OpenAPI document describes real response shapes, enforced.

Three layers of guard:

  * Every route declares a response schema (the 51-of-55 gap this closes).
  * The 422 documented everywhere is the app's own error envelope; FastAPI's
    default HTTPValidationError stub (a shape the app never emits) is gone.
  * For representative routes, the LIVE response's key set equals the schema's
    declared properties — catching both directions: a missing key would 500 at
    serialization, and a stale model silently *filtering* a real key would show
    up here as a declared-vs-live mismatch.

Plus the replay contract: `response_model` serialization applies to fresh
writes only; an idempotent replay returns the stored snapshot verbatim.
"""

import uuid

import pytest

from app.main import app

MUTATION_METHODS = {"post", "put", "delete", "patch"}


def _openapi() -> dict:
    return app.openapi()


def _operations():
    schema = _openapi()
    for path, item in schema["paths"].items():
        for method, op in item.items():
            if method in {"get", "post", "put", "delete", "patch"}:
                yield path, method, op


def _resolve(schema_doc: dict, ref_or_schema: dict) -> dict:
    if "$ref" in ref_or_schema:
        name = ref_or_schema["$ref"].rsplit("/", 1)[-1]
        return schema_doc["components"]["schemas"][name]
    return ref_or_schema


def _success_schema(schema_doc: dict, op: dict) -> dict:
    for status in ("200", "201"):
        if status in op["responses"]:
            content = op["responses"][status].get("content", {})
            return _resolve(schema_doc, content["application/json"]["schema"])
    raise AssertionError(f"no 200/201 response on {op.get('operationId')}")


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


# ---------------------------------------------------------------------------
# Document-level guarantees
# ---------------------------------------------------------------------------

def test_every_route_declares_a_response_schema():
    schema_doc = _openapi()
    shapeless = []
    for path, method, op in _operations():
        try:
            declared = _success_schema(schema_doc, op)
        except (AssertionError, KeyError):
            shapeless.append(f"{method.upper()} {path}")
            continue
        # An empty schema ({}), FastAPI's default for undeclared responses,
        # documents nothing — treat it as missing.
        if not declared:
            shapeless.append(f"{method.upper()} {path}")
    assert shapeless == [], f"routes documenting no response shape: {shapeless}"


def test_fastapi_validation_stub_is_gone():
    import json

    doc = json.dumps(_openapi())
    assert "HTTPValidationError" not in doc
    assert '"ValidationError"' not in doc


def test_422_documents_the_error_envelope():
    schema_doc = _openapi()
    for path, method, op in _operations():
        if path == "/health":
            continue  # unauthenticated, no error responses declared
        resp_422 = op["responses"].get("422")
        assert resp_422 is not None, f"422 undocumented on {method.upper()} {path}"
        declared = _resolve(schema_doc, resp_422["content"]["application/json"]["schema"])
        assert set(declared["properties"].keys()) == {"error"}, (
            f"422 on {method.upper()} {path} does not document the error envelope"
        )


# ---------------------------------------------------------------------------
# Live shape == declared shape (representative routes)
# ---------------------------------------------------------------------------

def _declared_keys(path: str, method: str) -> set:
    schema_doc = _openapi()
    op = schema_doc["paths"][path][method]
    declared = _success_schema(schema_doc, op)
    return set(declared["properties"].keys())


@pytest.mark.asyncio
async def test_category_live_shape_matches_declared(client):
    r = await client.post(
        "/v1/categories",
        json={"id": str(uuid.uuid4()), "name": f"shape-{uuid.uuid4().hex[:8]}",
              "color": "#0000FF"},
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    assert set(r.json().keys()) == _declared_keys("/v1/categories", "post")
    await client.delete(f"/v1/categories/{r.json()['id']}", headers=_idem())


@pytest.mark.asyncio
async def test_pagination_envelope_matches_declared(client):
    r = await client.get("/v1/categories")
    assert r.status_code == 200
    assert set(r.json().keys()) == _declared_keys("/v1/categories", "get")
    assert _declared_keys("/v1/categories", "get") == {"items", "total", "limit", "offset"}


@pytest.mark.asyncio
async def test_transaction_delete_live_shape_has_no_warnings(client, test_data):
    """DELETE returns the plain transaction shape. Its `warnings` key left
    with its only warning, which became a 409 block (bug 5.5) — the
    warnings channel's sole member is restore, which must still declare it.
    """
    txn_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"shape-del-{uuid.uuid4().hex[:8]}",
            "amount_cents": -777,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text

    d = await client.delete(f"/v1/transactions/{txn_id}", headers=_idem())
    assert d.status_code == 200, d.text
    declared = _declared_keys("/v1/transactions/{transaction_id}", "delete")
    assert "warnings" not in declared
    assert set(d.json().keys()) == declared
    assert "warnings" in _declared_keys(
        "/v1/transactions/{transaction_id}/restore", "post"
    )


# ---------------------------------------------------------------------------
# Replay contract: stored snapshot verbatim, same status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_returns_first_body_verbatim(client):
    key = str(uuid.uuid4())
    body = {"id": str(uuid.uuid4()), "name": f"replay-{uuid.uuid4().hex[:8]}",
            "color": "#00AA00"}

    first = await client.post(
        "/v1/categories", json=body, headers={"X-Idempotency-Key": key}
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/v1/categories", json=body, headers={"X-Idempotency-Key": key}
    )
    assert second.status_code == 201
    assert second.json() == first.json()

    await client.delete(f"/v1/categories/{body['id']}", headers=_idem())
