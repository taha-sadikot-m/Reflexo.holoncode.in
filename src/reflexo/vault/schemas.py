"""Vault JSONB models (BETA_02 §5)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CredentialMeta(BaseModel):
    """`credential_vault.meta`: what may be known about a secret without decrypting it."""

    token_type: str = ""
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    refresh_after: datetime | None = None
    issuer: str | None = None
