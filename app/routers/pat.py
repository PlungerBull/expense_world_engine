"""HTTP handlers for /auth/pat — thin adapters over helpers.pat.

Mounted alongside the main /auth router but kept in its own module so
that the PAT domain (auth middleware branching + revocation) stays
isolated from the user/settings bootstrap flow. Both routers share the
same /auth URL namespace.
"""

from typing import Optional

from fastapi import APIRouter, Header

from app.deps import CurrentUser
from app.helpers import pat as pat_service
from app.helpers.idempotency import run_idempotent
from app.schemas.pat import PatCreateRequest

router = APIRouter(prefix="/auth/pat", tags=["auth"])


@router.post("", status_code=201)
async def create_pat(
    body: PatCreateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    # The caller must be authenticated (via JWT — a freshly-minted
    # PAT cannot mint more PATs in v1, that's a future scoping concern
    # if/when admin-vs-user-token distinctions become relevant). For
    # now both token types can call this, and RLS scopes everything
    # to the caller's user_id regardless.
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=201,
        work=lambda conn: pat_service.create(conn, auth_user.id, body.name),
    )


@router.delete("/{pat_id}")
async def revoke_pat(
    pat_id: str,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: pat_service.revoke(conn, auth_user.id, pat_id),
    )
