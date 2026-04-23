from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, Header
from jose import JWTError, jwt

from app import db
from app.config import settings
from app.errors import unauthorized
from app.helpers.auth_token import PAT_PREFIX, hash_pat
from app.helpers.jwks import get_jwk


@dataclass
class AuthUser:
    id: str
    email: Optional[str]


# Algorithms the engine is willing to verify. ES256 is what modern
# Supabase projects issue by default; HS256 remains supported so
# legacy projects and our own test-generated tokens keep working. If
# an unrecognised alg arrives, we reject rather than silently trying
# to coerce it.
_ASYMMETRIC_ALGS = {"ES256", "RS256"}
_SYMMETRIC_ALGS = {"HS256"}


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

    # JWT path: alg-aware. Read the unverified header to pick the
    # right verification key before trusting any claims. Reading the
    # header is safe — it's not trusted, it only tells us which key
    # the signer *says* they used. The subsequent jwt.decode() is
    # what actually proves the signature.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError:
        raise unauthorized("Invalid token format.")

    alg = unverified_header.get("alg")

    if alg in _SYMMETRIC_ALGS:
        key = settings.supabase_jwt_secret
    elif alg in _ASYMMETRIC_ALGS:
        kid = unverified_header.get("kid")
        if not kid:
            raise unauthorized("Token missing key id.")
        key = get_jwk(kid)
        if key is None:
            raise unauthorized("Unknown signing key.")
    else:
        raise unauthorized("Unsupported signing algorithm.")

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[alg],
            options={"verify_aud": False},
        )
    except JWTError:
        raise unauthorized("Invalid or expired token.")

    sub = payload.get("sub")
    if not sub:
        raise unauthorized("Token missing subject claim.")

    return AuthUser(id=sub, email=payload.get("email"))


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
