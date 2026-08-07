"""Shared schema bases.

StrictModel is the request-model base: unknown fields 422 rather than being
silently dropped (CLAUDE.md fail-closed: "unknown input must 422"). Every
request model inherits it — INCLUDING nested request fragments
(TransferField, InboxTransferField): Pydantic model_config does not
propagate into nested models, so a plain-BaseModel fragment inside a strict
parent silently re-opens the hole for everything under its key.

Response models stay on BaseModel — the engine controls what it emits, and
strictness on output would guard nothing.
"""
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
