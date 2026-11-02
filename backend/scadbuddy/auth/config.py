"""The login configuration (#4975 phase 2, spec 2026-11-01 §9.2; D1, D3, D7): one row of
``auth_login_config``, its client secret sealed under the KEK.

The secret's AAD binds it to the issuer and the client id it was saved with, so a
changed issuer or client id needs the secret entered again: a saved secret is never
sent to an issuer it was not saved for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from scadbuddy.auth.store import Pool
from scadbuddy.core.secrets import Envelope, Kek, open_secret, seal_secret


class LoginConfig(BaseModel):
    """What the login runs with. Nothing in it is secret."""

    #: Off, the sign-in button is not offered and ``/auth/login`` refuses.
    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    #: D3: who may sign in. A subject listed here, or an ID token whose ``group_claim``
    #: holds ``group_value``; at least one is needed to switch the login on.
    allowed_subjects: list[str] = Field(default_factory=list, max_length=100)
    group_claim: str | None = None
    group_value: str | None = None
    #: D7: also end the IdP's session on sign-out, when it publishes how.
    rp_logout: bool = False

    @field_validator("issuer", "client_id")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()

    @field_validator("allowed_subjects")
    @classmethod
    def _subjects(cls, value: list[str]) -> list[str]:
        return sorted({subject.strip() for subject in value if subject.strip()})

    @field_validator("group_claim", "group_value")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    def has_rule(self) -> bool:
        return bool(self.allowed_subjects) or bool(self.group_claim and self.group_value)

    def allows(self, claims: Mapping[str, Any]) -> bool:
        """D3: whether these ID-token claims may sign in."""
        if claims.get("sub") in self.allowed_subjects:
            return True
        if not (self.group_claim and self.group_value):
            return False
        held = claims.get(self.group_claim)
        values = held if isinstance(held, list) else [held]
        return self.group_value in values


def _aad(config: LoginConfig) -> str:
    return f"auth_login_config.client_secret|{config.issuer}|{config.client_id}"


def seal(kek: Kek, config: LoginConfig, secret: str) -> Envelope:
    """``secret`` sealed for ``config``'s issuer and client id."""
    return seal_secret(kek, secret, _aad(config))


class LoginConfigStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def load(self) -> tuple[LoginConfig, Envelope | None]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT config, secret_sealed, dek_sealed, kek_id FROM auth_login_config"
            ).fetchone()
        if row is None:
            return LoginConfig(), None
        envelope = (
            Envelope(
                secret_sealed=bytes(row["secret_sealed"]),
                dek_sealed=bytes(row["dek_sealed"]),
                kek_id=row["kek_id"],
            )
            if row["secret_sealed"] is not None
            else None
        )
        return LoginConfig.model_validate(row["config"]), envelope

    def secret(self, kek: Kek, config: LoginConfig, envelope: Envelope) -> str:
        return open_secret(kek, envelope, _aad(config))

    def save(self, config: LoginConfig, envelope: Envelope | None) -> None:
        """Write ``config`` with its sealed secret (:func:`seal`), or none."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO auth_login_config (id, config, secret_sealed, dek_sealed, kek_id)"
                " VALUES (true, %(config)s, %(secret)s, %(dek)s, %(kek)s)"
                " ON CONFLICT (id) DO UPDATE SET config = EXCLUDED.config,"
                " secret_sealed = EXCLUDED.secret_sealed, dek_sealed = EXCLUDED.dek_sealed,"
                " kek_id = EXCLUDED.kek_id, updated_at = now()",
                {
                    "config": Jsonb(config.model_dump()),
                    "secret": None if envelope is None else envelope.secret_sealed,
                    "dek": None if envelope is None else envelope.dek_sealed,
                    "kek": None if envelope is None else envelope.kek_id,
                },
            )
