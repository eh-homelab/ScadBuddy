import base64
from typing import Any

import psycopg
import pytest

from scadbuddy_durable.credentials import (
    LEGACY_FORMAT_MESSAGE,
    credential_aad,
    credential_env,
    open_credential,
    usable_credentials,
)
from scadbuddy_durable.secrets import Envelope, SealError, kek_from_base64


def _envelope(v: dict[str, Any]) -> Envelope:
    return Envelope(
        secret_sealed=base64.b64decode(v["secret_sealed_b64"]),
        dek_sealed=base64.b64decode(v["dek_sealed_b64"]),
        kek_id=v["kek_id"],
    )


def test_aad_matches_json_stringify() -> None:
    assert (
        credential_aad("default", "anthropic_api_key", None)
        == 'ai_credentials:default:{"kind":"anthropic_api_key","base_url":null}'
    )
    assert credential_aad("x", "gateway", "https://h/é") == (
        'ai_credentials:x:{"kind":"gateway","base_url":"https://h/é"}'
    )


def test_opens_every_credential_vector(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    for v in vectors["credentials"]:
        args = dict(id=v["id"], priority=v["priority"], kind=v["kind"], base_url=v["base_url"])
        if v["opens"]:
            assert open_credential(kek, **args, envelope=_envelope(v)).secret == v["plaintext"]
        else:
            with pytest.raises(SealError, match=LEGACY_FORMAT_MESSAGE):
                open_credential(kek, **args, envelope=_envelope(v))


def test_an_edited_base_url_fails_authentication(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = next(c for c in vectors["credentials"] if c["kind"] == "gateway")
    with pytest.raises(SealError, match="failed authentication"):
        open_credential(
            kek,
            id=v["id"],
            priority=v["priority"],
            kind="gateway",
            base_url="https://attacker.example",
            envelope=_envelope(v),
        )


def test_env_per_kind(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    got = {}
    for v in vectors["credentials"]:
        if v["opens"]:
            c = open_credential(
                kek,
                id=v["id"],
                priority=v["priority"],
                kind=v["kind"],
                base_url=v["base_url"],
                envelope=_envelope(v),
            )
            got[v["name"]] = credential_env(c)
    assert got["api key, migrated row id"] == {
        "ANTHROPIC_API_KEY": "sk-ant-api03-vector-0000000000000000"
    }
    assert got["oauth token"] == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-vector-1111111111111111"}
    assert got["gateway with a path"] == {
        "ANTHROPIC_BASE_URL": "https://gw.example.com/anthropic/v1",
        "ANTHROPIC_AUTH_TOKEN": "gw-token-vector-2222222222222222",
    }


def test_the_secret_never_appears_in_repr(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = vectors["credentials"][0]
    c = open_credential(
        kek,
        id=v["id"],
        priority=v["priority"],
        kind=v["kind"],
        base_url=v["base_url"],
        envelope=_envelope(v),
    )
    assert v["plaintext"] not in repr(c)


async def _insert(conn: psycopg.AsyncConnection[Any], v: dict[str, Any], **over: Any) -> None:
    row = {
        "id": v["id"],
        "priority": v["priority"],
        "kind": v["kind"],
        "base_url": v["base_url"],
        "secret_sealed": base64.b64decode(v["secret_sealed_b64"]),
        "dek_sealed": base64.b64decode(v["dek_sealed_b64"]),
        "kek_id": v["kek_id"],
        "status": "active",
        "cooldown_until": None,
        **over,
    }
    await conn.execute(
        "INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed,"
        " kek_id, last4, status, cooldown_until)"
        " VALUES (%(id)s, %(priority)s, %(kind)s, %(base_url)s, %(secret_sealed)s,"
        " %(dek_sealed)s, %(kek_id)s, '', %(status)s, %(cooldown_until)s)",
        row,
    )


@pytest.mark.requires_postgres
async def test_usable_in_priority_order_skipping_what_cannot_be_used(
    agent_db: psycopg.AsyncConnection[Any], vectors: dict[str, Any]
) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    by = {v["name"]: v for v in vectors["credentials"]}
    # priority 0: disabled; 1: cooling down until the future; 2: cooled down already
    # (counts as active, Review Focus 3); 3: sealed under another key; 9: v1.
    await _insert(agent_db, by["api key, migrated row id"], status="disabled")
    await _insert(
        agent_db,
        by["oauth token"],
        status="cooling_down",
        cooldown_until="2999-01-01T00:00:00Z",
    )
    await _insert(
        agent_db,
        by["gateway with a path"],
        status="cooling_down",
        cooldown_until="2000-01-01T00:00:00Z",
    )
    await _insert(agent_db, by["gateway, non-ASCII host"], kek_id="0000000000000000")
    await _insert(agent_db, by["v1 credential (refused)"])
    usable = await usable_credentials(agent_db, kek)
    assert [c.id for c in usable] == [by["gateway with a path"]["id"]]


@pytest.mark.requires_postgres
async def test_an_edited_row_is_skipped_and_the_next_is_used(
    agent_db: psycopg.AsyncConnection[Any], vectors: dict[str, Any]
) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    by = {v["name"]: v for v in vectors["credentials"]}
    await _insert(
        agent_db, by["gateway with a path"], priority=0, base_url="https://attacker.example"
    )
    await _insert(agent_db, by["oauth token"], priority=1)
    usable = await usable_credentials(agent_db, kek)
    assert [c.kind for c in usable] == ["claude_oauth_token"]
