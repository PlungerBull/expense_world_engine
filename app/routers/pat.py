"""HTTP handlers for /auth/pat — thin adapters over helpers.pat.

Mounted alongside the main /auth router but kept in its own module so
that the PAT domain (auth middleware branching + revocation) stays
isolated from the user/settings bootstrap flow. Both routers share the
same /auth URL namespace.
"""

from uuid import UUID

from fastapi import APIRouter

from app.deps import CurrentUser, IdempotencyKey
from app.errors import ERROR_RESPONSES
from app.helpers import pat as pat_service
from app.helpers.idempotency import run_idempotent
from app.schemas.pat import PatCreateRequest, PatCreateResponse, PatResponse

router = APIRouter(prefix="/auth/pat", tags=["auth"], responses=ERROR_RESPONSES)


@router.post("", response_model=PatCreateResponse, status_code=201)
async def create_pat(
    body: PatCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    # The caller must hold a valid PAT (the JWT branch was deleted
    # 2026-08-03 — PATs are the only credential). Policy decision, kept
    # from v1: any authenticated PAT can mint more PATs — no
    # admin-vs-user token distinction exists yet; scoping becomes a
    # concern if one appears. Isolation is the engine-side user_id
    # passed to pat_service (RLS exists in the schema but is inert
    # under the owner connection — see CLAUDE.md "Tenant isolation").
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: pat_service.create(conn, auth_user.id, body.name),
        # The response carries the PAT plaintext, shown exactly once. With
        # permanent idempotency keys (sql/026) a stored snapshot would keep
        # it forever, cancelling "only the hash is stored" (bug 2.4).
        # Replays of this key answer 409 instead.
        store_snapshot=False,
    )


@router.delete("/{pat_id}", response_model=PatResponse)
async def revoke_pat(
    pat_id: UUID,
    auth_user: CurrentUser,
    x_idempotency_key: IdempotencyKey = None,
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: pat_service.revoke(conn, auth_user.id, pat_id),
    )
