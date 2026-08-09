from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas import StrictModel, owned_fields


class PatCreateRequest(StrictModel):
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
    #
    # PatCreateResponse deliberately restates PatResponse rather than
    # inheriting it: `token` sits at position 3 in the create response, and
    # a subclass field would land last (bloat-audit §17d, key-order rule in
    # schemas/__init__).
    common = dict(
        **owned_fields(row),
        token_prefix=row["token_prefix"],
        name=row["name"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )
    if plaintext is not None:
        return PatCreateResponse(token=plaintext, **common).model_dump(mode="json")
    return PatResponse(**common).model_dump(mode="json")
