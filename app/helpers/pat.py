"""Personal Access Token domain logic.

Service-layer functions for personal_access_tokens, called from
routers/pat.py. Routers stay thin (HTTP glue + idempotency) and delegate
business logic here.

Same convention as ``app/helpers/hashtags.py``: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction
boundaries (via ``run_idempotent``).
"""

import secrets
import uuid
from typing import Optional

import asyncpg

from app.constants import ActivityAction
from app.errors import not_found
from app.helpers.activity_log import write_activity_log
from app.helpers.auth_token import PAT_PREFIX, PAT_PREFIX_LEN, hash_pat
from app.schemas.pat import pat_from_row


async def create(
    conn: asyncpg.Connection,
    user_id: str,
    name: Optional[str],
) -> dict:
    """Mint a new PAT: generate plaintext, store hash, log creation, return once.

    The plaintext is in the response only — it is never written to the
    DB, never logged to activity_log, and never recoverable after this
    call returns. The activity_log snapshot deliberately omits
    ``token_hash`` and the plaintext so neither leaks through the audit
    trail.
    """
    # 32 random bytes → ~43-char urlsafe-b64 suffix. Full token length
    # is len(PAT_PREFIX) + 43 ≈ 51 chars, well above any brute-force
    # concern (256 bits of entropy).
    plaintext = f"{PAT_PREFIX}{secrets.token_urlsafe(32)}"
    token_hash = hash_pat(plaintext)
    token_prefix = plaintext[:PAT_PREFIX_LEN]
    pat_id = str(uuid.uuid4())

    row = await conn.fetchrow(
        """
        INSERT INTO personal_access_tokens
            (id, user_id, token_hash, token_prefix, name, created_at)
        VALUES ($1, $2, $3, $4, $5, now())
        RETURNING *
        """,
        pat_id,
        user_id,
        token_hash,
        token_prefix,
        name,
    )

    # Safe snapshot: excludes token_hash and plaintext. Only identity,
    # display prefix, label, and timestamps make it into activity_log.
    safe_snapshot = pat_from_row(row)

    await write_activity_log(
        conn, user_id, "personal_access_token", str(row["id"]),
        ActivityAction.CREATED,
        after_snapshot=safe_snapshot,
    )

    # Full response: includes the plaintext, shown exactly once.
    return pat_from_row(row, plaintext=plaintext)


async def revoke(
    conn: asyncpg.Connection,
    user_id: str,
    pat_id: str,
) -> dict:
    """Soft-delete (revoke) an active PAT and log the action.

    Active lookup ``WHERE revoked_at IS NULL`` means a second revoke on
    the same id surfaces as 404, matching the soft-delete convention
    used elsewhere (e.g. ``helpers.hashtags.delete_hashtag``).

    Raises:
        not_found: no active PAT with that id for this user.
    """
    before_row = await conn.fetchrow(
        """
        SELECT * FROM personal_access_tokens
        WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
        """,
        pat_id,
        user_id,
    )
    if before_row is None:
        raise not_found("personal_access_token")

    before = pat_from_row(before_row)

    after_row = await conn.fetchrow(
        """
        UPDATE personal_access_tokens
        SET revoked_at = now()
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        pat_id,
        user_id,
    )
    after = pat_from_row(after_row)

    await write_activity_log(
        conn, user_id, "personal_access_token", pat_id,
        ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
