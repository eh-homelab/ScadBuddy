"""Credential selection in pool order, as agent/src/credentials.ts and run.ts do it."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from scadbuddy_durable.secrets import Envelope, Kek, SealError, open_secret


class NoUsableCredential(Exception):
    """No row can be used. The message counts refusals and never carries a secret."""


@dataclass(frozen=True)
class Credential:
    kind: str
    secret: str = field(repr=False)
    base_url: str | None = None


def credential_aad(row_id: str, kind: str, base_url: str | None) -> str:
    body = json.dumps({"kind": kind, "base_url": base_url}, separators=(",", ":"), ensure_ascii=False)
    return f"ai_credentials:{row_id}:{body}"


def credential_env(credential: Credential) -> dict[str, str]:
    if credential.kind == "anthropic_api_key":
        return {"ANTHROPIC_API_KEY": credential.secret}
    if credential.kind == "claude_oauth_token":
        return {"CLAUDE_CODE_OAUTH_TOKEN": credential.secret}
    if credential.kind == "gateway" and credential.base_url is not None:
        return {"ANTHROPIC_BASE_URL": credential.base_url, "ANTHROPIC_AUTH_TOKEN": credential.secret}
    raise ValueError(f"unknown credential kind {credential.kind!r}")


# usable = the row's status admits it: active, or cooling down with the cooldown past.
_ROWS = """
SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id, status,
       (status = 'active' OR (status = 'cooling_down' AND cooldown_until <= now())) AS usable
FROM ai_credentials
ORDER BY priority
"""


class CredentialSource:
    def __init__(self, pool: AsyncConnectionPool, keks: Sequence[Kek]) -> None:
        self._pool = pool
        self._keks = {k.id: k for k in keks}

    async def secrets(self) -> list[str]:
        """Every credential's secret this worker can open, whatever its status: the ones
        a segment may have run with, which the projector redacts from what it logs
        (translate.py `redact`, as classic's turn does)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ROWS)
            rows = await cur.fetchall()
        found: list[str] = []
        for row_id, kind, base_url, secret_sealed, dek_sealed, kek_id, _status, _usable in rows:
            kek = self._keks.get(kek_id)
            if kek is None:
                continue
            try:
                found.append(
                    open_secret(
                        kek,
                        Envelope(bytes(secret_sealed), bytes(dek_sealed), kek_id),
                        credential_aad(row_id, kind, base_url),
                    )
                )
            except SealError:
                continue
        return found

    async def first_usable(self) -> Credential:
        disabled = cooling = other_key = 0
        async with self._pool.connection() as conn:
            cur = await conn.execute(_ROWS)
            rows = await cur.fetchall()
        for row_id, kind, base_url, secret_sealed, dek_sealed, kek_id, status, usable in rows:
            if not usable:
                if status == "disabled":
                    disabled += 1
                else:
                    cooling += 1
                continue
            kek = self._keks.get(kek_id)
            if kek is None:
                other_key += 1
                continue
            envelope = Envelope(bytes(secret_sealed), bytes(dek_sealed), kek_id)
            try:
                secret = open_secret(kek, envelope, credential_aad(row_id, kind, base_url))
            except SealError:
                other_key += 1
                continue
            return Credential(kind, secret, base_url)
        raise NoUsableCredential(
            f"no usable Claude credential: {disabled} disabled, {cooling} cooling down,"
            f" {other_key} sealed with another key"
        )
