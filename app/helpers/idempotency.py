"""Idempotency layer for write endpoints.

The public entry point is ``run_idempotent``. Every write handler calls
it once. A first-time write returns the plain body dict, which FastAPI
then serializes through the route's declared ``response_model`` — that is
what makes the OpenAPI shape declarations *enforced*, not decorative
(bug 10.1). A replay returns a pre-built ``JSONResponse`` reconstructed
from the stored snapshot, which deliberately bypasses the model: the
contract is "the original answer, verbatim".

Design notes:

* **Keys are permanent** (sql/026). A used ``X-Idempotency-Key`` returns
  its stored response forever — there is no TTL, no purge job, and no
  expired-key state. This mirrors the layer underneath it: ledger creates
  carry client-generated UUID primary keys (UUID-first), so the PK dedup
  never forgets either. One promise across both layers: a write you
  already made stays made, and asking again gets the original answer.
  (The 24h TTL this replaced never re-armed after expiry — bug 4.1 — so
  every post-expiry retry re-ran the write with no dedup at all.)

* **Replay requires the same request.** Each key stores a fingerprint —
  sha256 over (method, path, query, raw body) — and a replay whose
  fingerprint differs answers ``409 CONFLICT`` instead of returning a
  snapshot that belongs to some other request (fail closed: a client bug
  that reuses a key must be loud, not silently swallowed forever). The
  fingerprint is captured structurally by ``capture_request_fingerprint``,
  an app-wide dependency registered in ``main.py`` — no route passes it,
  so no route can forget it. Rows stored before sql/026 carry
  ``request_hash = ''`` and skip the comparison (grandfathered).

* The per-(user, key) lock is a Postgres transaction-scoped advisory
  lock (``pg_advisory_xact_lock``). Two concurrent requests with the
  same key serialize at the DB: the second blocks until the first
  commits, then reads the stored snapshot and returns it verbatim.
  No double writes possible, no race window.

* The snapshot captures both the body AND the HTTP status code. Replays
  reconstruct the full ``JSONResponse`` envelope from the database. On
  the fresh path the status comes from the route decorator's
  ``status_code=``, so the ``status_code`` argument here exists to be
  *stored* — keep the two in step (a mismatch would surface as replays
  answering with a different status than the first write).

* **One-time secrets are never snapshotted.** A route whose response
  contains a value that must not persist (the PAT plaintext — bug 2.4)
  passes ``store_snapshot=False``: the key row is still claimed (with its
  fingerprint), but the snapshot is stored as JSON ``null`` and a replay
  answers ``409 CONFLICT`` instead of re-serving the secret.

* Routes supply the write work as a callable ``work(conn) -> dict``.
  The helper owns the connection + transaction + lock + store so the
  handler stays pure glue.
"""

import hashlib
import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

import asyncpg
from fastapi import Request
from fastapi.responses import JSONResponse

from app import db
from app.errors import conflict


Work = Callable[[asyncpg.Connection], Awaitable[dict]]

# Set per-request by capture_request_fingerprint (app-wide dependency,
# registered in main.py). ContextVars propagate through the async call
# chain of the request's task, so run_idempotent reads the value without
# any route threading it through.
_request_fingerprint: ContextVar[Optional[str]] = ContextVar(
    "request_fingerprint", default=None
)


async def capture_request_fingerprint(request: Request) -> None:
    """App-wide dependency: fingerprint the raw request for idempotency.

    Hashes method, path, query string and raw body. Query params are
    included because they can change the stored response (e.g.
    ``?debit_as_negative=true``). Reading the body here is safe: Starlette
    caches it on the Request, so downstream model parsing reuses the same
    bytes. Runs on every route (GETs included, harmlessly) — registering
    it globally is what makes the fingerprint impossible to forget.
    """
    body = await request.body()
    digest = hashlib.sha256()
    for part in (
        request.method.encode(),
        request.url.path.encode(),
        request.url.query.encode(),
    ):
        digest.update(part)
        digest.update(b"\x00")
    digest.update(body)
    _request_fingerprint.set(digest.hexdigest())


def _lock_id(user_id: str, key: str) -> int:
    # Must fit in signed bigint for pg_advisory_xact_lock.
    digest = hashlib.sha256(f"{user_id}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@dataclass
class _Cached:
    body: dict
    status: int


async def _claim(
    conn: asyncpg.Connection,
    user_id: str,
    key: Optional[str],
    fingerprint: Optional[str],
) -> Optional[_Cached]:
    """Acquire the per-key lock and return any previously stored response.

    Must run as the first statement inside the write transaction. Returns
    None if this is a first-time write (caller proceeds with the work);
    returns a cached envelope if a prior request already completed.

    Raises:
        conflict (409): the key exists but was used for a different
            request (fingerprint mismatch), or its response holds a
            one-time secret that is deliberately not replayable.
    """
    if key is None:
        return None
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1)", _lock_id(user_id, key)
    )
    row = await conn.fetchrow(
        """
        SELECT response_snapshot, response_status, request_hash
        FROM idempotency_keys
        WHERE user_id = $1 AND key = $2
        """,
        user_id,
        key,
    )
    if row is None:
        return None
    # '' = row stored before sql/026 introduced fingerprints; comparison
    # is skipped so a legitimate pre-migration retry still replays.
    if row["request_hash"] and row["request_hash"] != fingerprint:
        raise conflict(
            "This idempotency key was already used for a different request. "
            "Send one unique key per intended write."
        )
    body = json.loads(row["response_snapshot"])
    if body is None:
        # store_snapshot=False path: the original response carried a
        # one-time secret (PAT plaintext) and is not replayable.
        raise conflict(
            "This idempotency key was already used, and the original "
            "response contained a one-time secret that cannot be replayed. "
            "Retry with a new key to perform a new write."
        )
    return _Cached(body=body, status=int(row["response_status"]))


async def _store(
    conn: asyncpg.Connection,
    user_id: str,
    key: Optional[str],
    fingerprint: Optional[str],
    body: Optional[dict],
    status: int,
) -> None:
    """Persist the final response envelope for this key.

    Called inside the same transaction as the write, just before commit.
    ``body=None`` stores JSON ``null`` — the claimed-but-not-replayable
    marker for one-time-secret responses. On conflict (shouldn't happen
    under the advisory lock, but belt and braces) the insert is a no-op;
    with permanent keys there is no expired-row state a conflict could
    mask, so DO NOTHING is now exactly right rather than bug 4.1's trap.
    """
    if key is None:
        return
    await conn.execute(
        """
        INSERT INTO idempotency_keys
            (id, key, user_id, response_snapshot, response_status, request_hash, created_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, now())
        ON CONFLICT (user_id, key) DO NOTHING
        """,
        str(uuid.uuid4()),
        key,
        user_id,
        json.dumps(body),
        status,
        fingerprint,
    )


async def run_idempotent(
    user_id: str,
    key: Optional[str],
    status_code: int,
    work: Work,
    store_snapshot: bool = True,
) -> Union[dict, JSONResponse]:
    """Run a write under the idempotency guard.

    Acquires a pooled connection, opens a transaction, claims the per-key
    advisory lock, runs ``work(conn)`` inside the same transaction, stores
    the response envelope (body + status + request fingerprint), and
    returns the body dict for FastAPI to serialize through the route's
    ``response_model``. Cached hits skip ``work`` entirely and return the
    stored envelope verbatim as a pre-built ``JSONResponse`` (a Response
    return bypasses response-model serialization — intended: a replay is
    the original answer, not a re-render of it).

    ## Transaction boundaries and locks — the convention for every service helper

    **This is the only transaction boundary in the engine's write path.** Every
    mutating route passes a ``work=lambda conn: <service>(conn, ...)`` closure
    into this function, so a service helper's row write, its junction writes and
    its ``activity_log`` entry are always in one transaction with the
    idempotency-key claim. That is where "all or nothing" actually comes from —
    it is structural, not something each call site upholds.

    Consequently, service helpers in ``app/helpers/`` do **NOT** open their own
    ``conn.transaction()``. They assume they are already inside one and that the
    caller has acquired any ``FOR UPDATE`` locks it needs. Opening a nested
    transaction there would create a savepoint whose rollback semantics nobody
    designed for.

    Locks are taken on the row being *modified* — ``expense_transactions`` in the
    update/delete/restore paths, the inbox row on promote — never on the account.
    Their purpose is to keep a read-modify-write sequence coherent: the
    before/after pair written to ``activity_log`` must describe one state of the
    row, and the transfer-pair invariants must see a stable sibling.

    (Until sql/022 this convention was documented in ``app/helpers/balance.py``
    and cross-referenced from five other modules. It lived there because the
    stored-balance UPDATE was the thing most obviously depending on it. The
    balance is computed now, so the contract moved to the function that actually
    owns the boundary.)

    Args:
        user_id: Authenticated user id from the PAT.
        key: ``X-Idempotency-Key`` header value, or None when absent.
        status_code: HTTP status for the first-time response (stored
            alongside the body for replays).
        work: ``async def(conn) -> dict`` — the write body. Must return
            a dict that's already JSON-serializable (Pydantic's
            ``model_dump(mode="json")`` output).
        store_snapshot: False for responses carrying a one-time secret
            (PAT plaintext — bug 2.4). The key is still claimed and
            fingerprinted, but replays answer 409 instead of re-serving
            the secret.
    """
    fingerprint = _request_fingerprint.get()
    if key is not None and fingerprint is None:
        # Programming error, not client error: the app-wide dependency
        # (main.py) was not registered or this was called outside a
        # request. Fail loudly rather than storing an unfingerprinted key.
        raise RuntimeError(
            "run_idempotent called with an idempotency key but no request "
            "fingerprint; capture_request_fingerprint must be registered "
            "as an app-wide dependency."
        )
    async with db.pool.acquire() as conn, conn.transaction():
        cached = await _claim(conn, user_id, key, fingerprint)
        if cached is not None:
            return JSONResponse(content=cached.body, status_code=cached.status)
        response = await work(conn)
        await _store(
            conn,
            user_id,
            key,
            fingerprint,
            response if store_snapshot else None,
            status_code,
        )
        return response
