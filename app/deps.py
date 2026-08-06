"""Request authentication.

**PAT-only, by deliberate deletion (2026-08-03).** Every token the engine
accepts is an engine-issued Personal Access Token: an opaque secret prefixed
``ewe_pat_``, looked up by SHA-256 hash and revocable by setting
``revoked_at``. There is no second path.

There used to be one. A JWT branch verified Supabase Auth tokens, choosing
its key from the algorithm named *in the token's own header* — so a token
declaring ``alg: HS256`` was verified against ``settings.supabase_jwt_secret``,
whose value was the literal string ``local-unused``, published in
``.env.example``. Forging ``jwt.encode({"sub": <any-uuid>}, "local-unused",
"HS256")`` yielded full read/write as any user, and no expiry was required.
Audit finding 2.1.

It is gone rather than gated because the local profile never used it: the
owner authenticates with a PAT, and the JWT path existed only to serve the
mothballed cloud profile. A flag defaulting to off would have left the
vulnerable code one config edit away from live. Restoring it for a cloud
reactivation is a deliberate, reviewable act — see ``deploy/cloud/README.md``,
which records what has to be rebuilt and the three mistakes not to repeat.
"""

from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, Header

from app import db
from app.errors import unauthorized
from app.helpers.auth_token import PAT_PREFIX, hash_pat


@dataclass
class AuthUser:
    id: str


async def get_current_user(
    authorization: Optional[str] = Header(None),
) -> AuthUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized()

    token = authorization.removeprefix("Bearer ").strip()

    # Fail closed: anything that is not a well-formed PAT is rejected before
    # a single byte of it is interpreted. No fallback branch, no algorithm
    # negotiation, nothing the caller can steer.
    if not token.startswith(PAT_PREFIX):
        raise unauthorized("Invalid or revoked token.")

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id FROM personal_access_tokens
            WHERE token_hash = $1 AND revoked_at IS NULL
            """,
            hash_pat(token),
        )
    if row is None:
        raise unauthorized("Invalid or revoked token.")

    return AuthUser(id=str(row["user_id"]))


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
