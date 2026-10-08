"""The Claude credential, read as the agent reads it (spec 2026-10-01 §6.2).

Ports agent/src/credentials.ts `credentialAad` and `openCredential`, the pool's
"usable, in priority order" (`CredentialPool`, agent/src/harness/fallback.ts) and
agent/src/harness/run.ts `credentialEnv`. The agent alone writes `ai_credentials`:
health verdicts (cooldowns, disables) stay its job until 5c decides how a durable
segment reports one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import psycopg

from scadbuddy_durable.secrets import SEAL_V1, Envelope, Kek, SealError, open_secret, sealed_version

log = logging.getLogger(__name__)

CredentialKind = Literal["anthropic_api_key", "claude_oauth_token", "gateway"]

LEGACY_FORMAT_MESSAGE = (
    "the stored credential was saved in an older format that did not bind its kind and"
    " base URL; save it again"
)


@dataclass(frozen=True)
class Credential:
    id: str
    priority: int
    kind: CredentialKind
    base_url: str | None
    secret: str = field(repr=False)

    @property
    def label(self) -> str:
        """For logs: which credential, never its secret (credentials.ts `credentialLabel`)."""
        what = {"gateway": f"gateway {self.base_url or ''}", "claude_oauth_token": "OAuth token"}
        return f"credential {self.priority + 1} ({what.get(self.kind, 'API key')})"


def credential_aad(id: str, kind: str, base_url: str | None) -> str:
    # JSON.stringify: keys in this order, no spaces, non-ASCII left as is.
    bound = json.dumps(
        {"kind": kind, "base_url": base_url}, separators=(",", ":"), ensure_ascii=False
    )
    return f"ai_credentials:{id}:{bound}"


def open_credential(
    kek: Kek,
    *,
    id: str,
    priority: int,
    kind: CredentialKind,
    base_url: str | None,
    envelope: Envelope,
) -> Credential:
    if sealed_version(envelope.secret_sealed) == SEAL_V1:
        raise SealError(LEGACY_FORMAT_MESSAGE)
    secret = open_secret(kek, envelope, credential_aad(id, kind, base_url))
    if kind == "gateway" and base_url is None:
        raise SealError("gateway credential has no base_url")
    return Credential(id=id, priority=priority, kind=kind, base_url=base_url, secret=secret)


def credential_env(c: Credential) -> dict[str, str]:
    if c.kind == "anthropic_api_key":
        return {"ANTHROPIC_API_KEY": c.secret}
    if c.kind == "claude_oauth_token":
        return {"CLAUDE_CODE_OAUTH_TOKEN": c.secret}
    assert c.base_url is not None
    return {"ANTHROPIC_BASE_URL": c.base_url, "ANTHROPIC_AUTH_TOKEN": c.secret}


async def usable_credentials(conn: psycopg.AsyncConnection[Any], kek: Kek) -> list[Credential]:
    """Every credential a query may use now, in priority order.

    Usable is the agent's rule: active, or cooling down with its cooldown passed;
    sealed under the mounted key; in the current format; and authenticating against
    its row. A row that fails any of those is skipped and logged by label.
    """
    cur = await conn.execute(
        "SELECT id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id"
        " FROM ai_credentials"
        " WHERE status = 'active' OR (status = 'cooling_down' AND cooldown_until <= now())"
        " ORDER BY priority"
    )
    usable: list[Credential] = []
    for id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id in await cur.fetchall():
        envelope = Envelope(
            secret_sealed=bytes(secret_sealed), dek_sealed=bytes(dek_sealed), kek_id=kek_id
        )
        if kek_id != kek.id:
            log.info("credential %d is sealed under another key; skipped", priority + 1)
            continue
        try:
            usable.append(
                open_credential(
                    kek,
                    id=id,
                    priority=priority,
                    kind=cast(CredentialKind, kind),
                    base_url=base_url,
                    envelope=envelope,
                )
            )
        except SealError as err:
            log.warning("credential %d cannot be opened (%s); skipped", priority + 1, err)
    return usable
