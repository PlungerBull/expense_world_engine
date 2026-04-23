from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, Header
from jose import JWTError, jwt

from app import db
from app.config import settings
from app.errors import unauthorized
from app.helpers.auth_token import PAT_PREFIX, hash_pat


@dataclass
class AuthUser:
    id: str
    email: Optional[str]


async def get_current_user(
    authorization: Optional[str] = Header(None),
) -> AuthUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized()

    token = authorization.removeprefix("Bearer ").strip()

    # PAT path: opaque engine-issued secret. Recognize by prefix so
    # PATs and JWTs don't have to be distinguished by try/catch on
    # signature verification — the prefix is unambiguous.
    if token.startswith(PAT_PREFIX):
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
        return AuthUser(id=str(row["user_id"]), email=None)

    # JWT path: Supabase-issued session token. Verified against the
    # shared HS256 secret; the sub claim carries the user_id.
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except JWTError:
        raise unauthorized("Invalid or expired token.")

    sub = payload.get("sub")
    if not sub:
        raise unauthorized("Token missing subject claim.")

    return AuthUser(id=sub, email=payload.get("email"))


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
