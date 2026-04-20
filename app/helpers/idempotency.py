"""Idempotency layer for write endpoints.

The public entry point is ``run_idempotent``. Every write handler calls
it once and gets back a ``JSONResponse`` with the correct status code,
whether the request is a first-time write or a replay.

Design notes:

* The per-(user, key) lock is a Postgres transaction-scoped advisory
  lock (``pg_advisory_xact_lock``). Two concurrent requests with the
  same key serialize at the DB: the second blocks until the first
  commits, then reads the stored snapshot and returns it verbatim.
  No double writes possible, no race window.

* The snapshot captures both the body AND the HTTP status code. Replays
  reconstruct the full ``JSONResponse`` envelope from the database, so a
  future handler that forgets to wrap the result in ``JSONResponse(...,
  status_code=201)`` can't silently downgrade replay to 200.

* Routes supply the write work as a callable ``work(conn) -> dict``.
  The helper owns the connection + transaction + lock + store so the
  handler stays pure glue.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import asyncpg
from fastapi.responses import JSONResponse

from app import db


Work = Callable[[asyncpg.Connection], Awaitable[dict]]


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
) -> Optional[_Cached]:
    """Acquire the per-key lock and return any previously stored response.

    Must run as the first statement inside the write transaction. Returns
    None if this is a first-time write (caller proceeds with the work);
    returns a cached envelope if a prior request already completed.
    """
    if key is None:
        return None
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1)", _lock_id(user_id, key)
    )
    row = await conn.fetchrow(
        """
        SELECT response_snapshot, response_status FROM idempotency_keys
        WHERE user_id = $1 AND key = $2 AND expires_at > now()
        """,
        user_id,
        key,
    )
    if row is None:
        return None
    return _Cached(
        body=json.loads(row["response_snapshot"]),
        status=int(row["response_status"]),
    )


async def _store(
    conn: asyncpg.Connection,
    user_id: str,
    key: Optional[str],
    body: dict,
    status: int,
) -> None:
    """Persist the final response envelope for this key.

    Called inside the same transaction as the write, just before commit.
    On conflict (shouldn't happen under the advisory lock, but belt and
    braces) the insert is a no-op.
    """
    if key is None:
        return
    await conn.execute(
        """
        INSERT INTO idempotency_keys
            (id, key, user_id, processed_at, response_snapshot, response_status, expires_at, created_at)
        VALUES ($1, $2, $3, now(), $4::jsonb, $5, now() + interval '24 hours', now())
        ON CONFLICT (user_id, key) DO NOTHING
        """,
        str(uuid.uuid4()),
        key,
        user_id,
        json.dumps(body),
        status,
    )


async def run_idempotent(
    user_id: str,
    key: Optional[str],
    status_code: int,
    work: Work,
) -> JSONResponse:
    """Run a write under the idempotency guard.

    Acquires a pooled connection, opens a transaction, claims the per-key
    advisory lock, runs ``work(conn)`` inside the same transaction, stores
    the response envelope (body + status), and returns a ``JSONResponse``.
    Cached hits skip ``work`` entirely and return the stored envelope
    verbatim.

    Args:
        user_id: Authenticated user id from the JWT.
        key: ``X-Idempotency-Key`` header value, or None when absent.
        status_code: HTTP status for the first-time response (stored
            alongside the body for replays).
        work: ``async def(conn) -> dict`` — the write body. Must return
            a dict that's already JSON-serializable (Pydantic's
            ``model_dump(mode="json")`` output).
    """
    async with db.pool.acquire() as conn, conn.transaction():
        cached = await _claim(conn, user_id, key)
        if cached is not None:
            return JSONResponse(content=cached.body, status_code=cached.status)
        response = await work(conn)
        await _store(conn, user_id, key, response, status_code)
        return JSONResponse(content=response, status_code=status_code)
