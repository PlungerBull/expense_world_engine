"""Shared schema bases and serializer kwargs helpers.

StrictModel is the request-model base: unknown fields 422 rather than being
silently dropped (CLAUDE.md fail-closed: "unknown input must 422"). Every
request model inherits it — and any future nested request fragment must
inherit it ITSELF: Pydantic model_config does not propagate into nested
models, so a plain-BaseModel fragment inside a strict parent silently
re-opens the hole for everything under its key. (No nested fragment exists
today — the last two, the transfer fragments, left with the 2026-08-10
removal, taking the only nested-strictness test pins with them; the next
fragment must bring its own pin.)

Response models stay on BaseModel — the engine controls what it emits, and
strictness on output would guard nothing.

The ``*_from_row`` serializers share their head (str-ified id/user_id) and
audit tail (created_at/updated_at/version/deleted_at) via the kwargs helpers
below — deliberately NOT via Pydantic base classes: inherited fields keep
the base's position in ``model_fields``, so OwnedResource/AuditedResource
bases would reorder the JSON keys of six response models (audit tail jumping
from last to third), and redeclaring a field in the subclass does not move
it back. Kwargs splats leave field order — and therefore the emitted bytes —
untouched (bloat-audit §17b, verified on this pydantic).
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def owned_fields(row) -> dict:
    """The id/user_id head every owned-resource serializer str-ifies."""
    return {"id": str(row["id"]), "user_id": str(row["user_id"])}


def audit_fields(row) -> dict:
    """The created_at/updated_at/version/deleted_at tail of audited resources."""
    return {
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": row["version"],
        "deleted_at": row["deleted_at"],
    }


def opt_id(value) -> Optional[str]:
    """str-ify a nullable UUID column; ``None`` stays ``None``.

    ``is not None``, not truthiness — the engine idiom. (UUIDs are always
    truthy, even the nil UUID, so the old ``if row[x]`` sites were not a
    live bug — but a falsy-but-present value in a future adopter would be
    silently nulled, which is exactly the class of drift the idiom exists
    to prevent.)
    """
    return str(value) if value is not None else None
