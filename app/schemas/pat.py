from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PatCreateRequest(BaseModel):
    name: Optional[str] = None


class PatCreateResponse(BaseModel):
    id: str
    user_id: str
    token: str
    token_prefix: str
    name: Optional[str]
    created_at: datetime
    revoked_at: Optional[datetime] = None


class PatResponse(BaseModel):
    id: str
    user_id: str
    token_prefix: str
    name: Optional[str]
    created_at: datetime
    revoked_at: Optional[datetime] = None


def pat_from_row(row, plaintext: Optional[str] = None) -> dict:
    # When plaintext is supplied (only on create), the full response
    # including the one-shot token is returned. On every other path
    # the token is never reconstructable — only the hash is stored.
    if plaintext is not None:
        return PatCreateResponse(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            token=plaintext,
            token_prefix=row["token_prefix"],
            name=row["name"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        ).model_dump(mode="json")
    return PatResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        token_prefix=row["token_prefix"],
        name=row["name"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    ).model_dump(mode="json")
