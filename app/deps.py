from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, Header
from jose import JWTError, jwt

from app.config import settings
from app.errors import unauthorized


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
