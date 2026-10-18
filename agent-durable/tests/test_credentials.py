import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from scadbuddy_durable.credentials import (
    LEGACY_FORMAT_MESSAGE,
    cooldown_until,
    credential_aad,
    credential_env,
    open_credential,
    rate_limit_reset,
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


def test_an_unknown_kind_is_refused_not_treated_as_a_gateway(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = next(c for c in vectors["credentials"] if c["kind"] == "gateway")
    with pytest.raises(SealError, match="unknown credential kind"):
        open_credential(
            kek,
            id=v["id"],
            priority=v["priority"],
            kind="future_kind",  # type: ignore[arg-type]
            base_url=v["base_url"],
            envelope=_envelope(v),
        )


NOW = datetime(2026, 10, 17, 22, 16, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "at"),
    [
        # #4088: the four results measured in prod on 2026-10-17 at 22:16 UTC.
        ("You've hit your weekly limit · resets 1pm (UTC)", datetime(2026, 10, 18, 13, 0)),
        ("You've hit your session limit · resets 10:30pm (UTC)", datetime(2026, 10, 17, 22, 30)),
        ("You've hit your weekly limit · resets 11pm (UTC)", datetime(2026, 10, 17, 23, 0)),
        (
            "You've hit your weekly limit · resets Oct 21, 4am (UTC)",
            datetime(2026, 10, 21, 4, 0),
        ),
        ("API Error: 429 · resets 12am (UTC)", datetime(2026, 10, 18, 0, 0)),
        ("resets 12pm (UTC)", datetime(2026, 10, 18, 12, 0)),
        ("resets 9:30PM (utc)", datetime(2026, 10, 18, 21, 30)),
    ],
)
def test_a_rate_limit_reset_is_read_from_the_result_text(text: str, at: datetime) -> None:
    """#4088: the reset Claude Code's result names, the next such time in UTC."""
    assert rate_limit_reset(text, NOW) == at.replace(tzinfo=UTC)


def test_a_time_only_reset_rolls_past_midnight_and_a_dated_one_past_year_end() -> None:
    late = datetime(2026, 12, 31, 23, 50, tzinfo=UTC)
    assert rate_limit_reset("resets 1:05am (UTC)", late) == datetime(2027, 1, 1, 1, 5, tzinfo=UTC)
    assert rate_limit_reset("resets Jan 2, 4am (UTC)", late) == datetime(
        2027, 1, 2, 4, 0, tzinfo=UTC
    )
    assert rate_limit_reset("resets Dec 31, 11:55pm (UTC)", late) == datetime(
        2026, 12, 31, 23, 55, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "text",
    [
        # The text names the minute only: one just passed is passing now, not
        # tomorrow's or next year's (#4093 review).
        "resets 10:16pm (UTC)",
        "resets 10:15pm (UTC)",
        "resets 10:02pm (UTC)",
        "resets Oct 17, 10:15pm (UTC)",
        "resets Oct 17, 10pm (UTC)",
        # A date earlier this year has passed; it is not next year's.
        "resets Sep 3, 4am (UTC)",
        "resets Jan 2, 4am (UTC)",
    ],
)
def test_a_reset_that_has_just_passed_keeps_the_default_cooldown(text: str) -> None:
    assert rate_limit_reset(text, NOW) is None
    assert cooldown_until(NOW, rate_limit_reset(text, NOW)) == NOW + timedelta(seconds=60)


def test_a_time_only_reset_past_the_grace_is_tomorrows() -> None:
    assert rate_limit_reset("resets 10pm (UTC)", NOW) == datetime(2026, 10, 18, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "API Error: 429 rate limited",
        "resets soon",
        "resets 1pm (PST)",
        "resets 13pm (UTC)",
        "resets 0am (UTC)",
        "resets 10:75pm (UTC)",
        "resets Foo 21, 4am (UTC)",
        "resets Feb 30, 4am (UTC)",
    ],
)
def test_text_naming_no_reset_keeps_the_default_cooldown(text: str) -> None:
    assert rate_limit_reset(text, NOW) is None
    assert cooldown_until(NOW, rate_limit_reset(text, NOW)) == NOW + timedelta(seconds=60)


def test_a_named_reset_is_still_held_to_a_day() -> None:
    until = rate_limit_reset("resets Oct 21, 4am (UTC)", NOW)
    assert cooldown_until(NOW, until) == NOW + timedelta(hours=24)
