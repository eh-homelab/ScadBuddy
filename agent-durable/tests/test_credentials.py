from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from scadbuddy_durable.credentials import (
    Credential,
    CredentialSource,
    NoUsableCredential,
    credential_aad,
    credential_env,
)
from scadbuddy_durable.secrets import Kek, kek_from_base64, seal_bytes

VECTORS = json.loads((Path(__file__).parents[2] / "agent/test/fixtures/secret-vectors.json").read_text())
KEK = kek_from_base64(VECTORS["kek_base64"])
PREVIOUS = Kek(id="prev0000prev0000", key=os.urandom(32))
FOREIGN = Kek(id="foreign000000000", key=os.urandom(32))
GATEWAY = "https://gateway.example/v1"


def test_aad_matches_the_typescript_vector() -> None:
    gw = next(v for v in VECTORS["vectors"] if v["name"] == "gateway.v2")
    assert credential_aad("c-3", "gateway", gw["base_url"]) == gw["aad"]
    assert (
        credential_aad("default", "anthropic_api_key", None)
        == next(v for v in VECTORS["vectors"] if v["name"] == "anthropic_api_key.v2")["aad"]
    )


def test_env_per_kind() -> None:
    assert credential_env(Credential("anthropic_api_key", "s", None)) == {"ANTHROPIC_API_KEY": "s"}
    assert credential_env(Credential("claude_oauth_token", "s", None)) == {"CLAUDE_CODE_OAUTH_TOKEN": "s"}
    assert credential_env(Credential("gateway", "s", GATEWAY)) == {
        "ANTHROPIC_BASE_URL": GATEWAY,
        "ANTHROPIC_AUTH_TOKEN": "s",
    }


@pytest_asyncio.fixture
async def pool(pg_conninfo: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(pg_conninfo, open=False) as p:
        yield p


async def insert(
    pool: AsyncConnectionPool,
    row_id: str,
    priority: int,
    secret: str,
    *,
    kind: str = "anthropic_api_key",
    base_url: str | None = None,
    status: str = "active",
    cooldown: str | None = None,
    kek: Kek = KEK,
) -> None:
    aad = credential_aad(row_id, kind, base_url)
    dek = os.urandom(32)
    secret_sealed = seal_bytes(dek, secret.encode(), aad)
    dek_sealed = seal_bytes(kek.key, dek, f"dek:{aad}")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ai_credentials (id, kind, base_url, secret_sealed, dek_sealed, kek_id, last4,"
            " priority, status, cooldown_until)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s::text IS NULL THEN NULL ELSE now() + %s::interval END)",
            (
                row_id,
                kind,
                base_url,
                secret_sealed,
                dek_sealed,
                kek.id,
                "xxxx",
                priority,
                status,
                cooldown,
                cooldown,
            ),
        )


@pytest.mark.requires_postgres
async def test_picks_the_first_usable_in_priority_order(pool: AsyncConnectionPool) -> None:
    await insert(pool, "a", 0, "sk-a", status="disabled")
    await insert(pool, "b", 1, "sk-b", status="cooling_down", cooldown="1 hour")
    await insert(pool, "c", 2, "sk-c", kek=FOREIGN)
    await insert(pool, "d", 3, "sk-d", kind="gateway", base_url=GATEWAY)
    got = await CredentialSource(pool, [KEK]).first_usable()
    assert got == Credential("gateway", "sk-d", GATEWAY)


@pytest.mark.requires_postgres
async def test_an_expired_cooldown_is_usable(pool: AsyncConnectionPool) -> None:
    await insert(pool, "a", 0, "sk-a", status="cooling_down", cooldown="-1 hour")
    got = await CredentialSource(pool, [KEK]).first_usable()
    assert got == Credential("anthropic_api_key", "sk-a", None)


@pytest.mark.requires_postgres
async def test_a_row_under_the_previous_kek_opens(pool: AsyncConnectionPool) -> None:
    await insert(pool, "a", 0, "sk-old", kek=PREVIOUS, kind="claude_oauth_token")
    got = await CredentialSource(pool, [KEK, PREVIOUS]).first_usable()
    assert got == Credential("claude_oauth_token", "sk-old", None)


@pytest.mark.requires_postgres
async def test_no_usable_row_names_each_refusal_without_the_secret(pool: AsyncConnectionPool) -> None:
    await insert(pool, "a", 0, "sk-secret-a", status="disabled")
    await insert(pool, "b", 1, "sk-secret-b", status="cooling_down", cooldown="1 hour")
    await insert(pool, "c", 2, "sk-secret-c", kek=FOREIGN)
    with pytest.raises(NoUsableCredential) as err:
        await CredentialSource(pool, [KEK]).first_usable()
    assert str(err.value) == (
        "no usable Claude credential: 1 disabled, 1 cooling down, 1 sealed with another key"
    )
    assert "sk-secret" not in str(err.value)


@pytest.mark.requires_postgres
async def test_an_empty_table_raises(pool: AsyncConnectionPool) -> None:
    with pytest.raises(NoUsableCredential):
        await CredentialSource(pool, [KEK]).first_usable()


@pytest.mark.requires_postgres
async def test_every_secret_that_opens_is_one_to_redact(pool: AsyncConnectionPool) -> None:
    # Whichever credential a segment ran with, even one disabled since: never a foreign one.
    await insert(pool, "a", 0, "sk-secret-a", status="disabled")
    await insert(pool, "b", 1, "sk-secret-b", status="cooling_down", cooldown="1 hour")
    await insert(pool, "c", 2, "sk-secret-c", kek=FOREIGN)
    await insert(pool, "d", 3, "sk-secret-d", kind="gateway", base_url=GATEWAY)
    assert sorted(await CredentialSource(pool, [KEK]).secrets()) == [
        "sk-secret-a",
        "sk-secret-b",
        "sk-secret-d",
    ]
