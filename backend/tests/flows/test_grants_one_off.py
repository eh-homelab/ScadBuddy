"""A fetch from a host off the asset allowlist is outside every run grant (#959): a
person approves each one, whatever a standing grant for fetch_asset admits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scadbuddy.flows.grants import (
    NEVER_GRANTED,
    CallFacts,
    Grant,
    GrantSpec,
    Outside,
    choose,
    match_grant,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
URL = {"slug": "box", "url": "https://icons.example.com/a.svg"}
ONCE = {**URL, "allow_host": "icons.example.com"}


def _standing() -> Grant:
    """A standing grant for every fetch_asset call, unbounded."""
    return Grant(
        id="g1",
        run_id="r1",
        status="approved",
        spec=GrantSpec.model_validate(
            {
                "expires_at": NOW + timedelta(hours=1),
                "rules": [{"fn": "tool", "tool": "fetch_asset"}],
            }
        ),
        requested_by={"kind": "browser"},
        expires_at=NOW + timedelta(hours=1),
        created_at=NOW,
    )


def test_a_standing_grant_admits_a_plain_fetch_but_never_a_one_off_host() -> None:
    grant = _standing()
    plain = CallFacts(fn="tool", tool="fetch_asset", args=URL)
    assert choose(grant, plain, narrowing=None, narrowed_by=None, now=NOW) == (0, None)
    for tool in ("fetch_asset", "scadbuddy__fetch_asset"):
        once = CallFacts(fn="tool", tool=tool, args=ONCE)
        assert choose(grant, once, narrowing=None, narrowed_by=None, now=NOW) == NEVER_GRANTED


async def test_match_grant_parks_it_without_reading_or_spending_the_grant() -> None:
    grant = _standing()
    spent: list[str] = []

    class Store:
        async def grant_use(self, request_id: str) -> None:
            raise AssertionError("no prior use is read for it")

        async def get_run(self, run_id: str) -> None:
            return None

        async def get_grant(self, run_id: str) -> Grant:
            return grant

        async def consume(self, run_id: str, request: Any) -> str:
            spent.append(request.request_id)
            return "spent"

    async def facts_of(fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
        return CallFacts(fn=fn, tool=tool, args=args)

    verdict = await match_grant(
        Store(),
        facts_of,
        lambda fn, tool, args: "hash",
        run_id="r1",
        request_id="q1",
        fn="tool",
        tool="fetch_asset",
        args=ONCE,
        now=NOW,
    )
    assert isinstance(verdict, Outside)
    assert (verdict.reason, verdict.parent_hop) == (NEVER_GRANTED, False)
    assert spent == []
