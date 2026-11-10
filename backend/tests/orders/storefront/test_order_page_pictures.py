"""The order page's picture copies (#5965 review), with no database: the copies are
bounded and each blob key is copied once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.orders.storefront import order_page
from scadbuddy.orders.storefront.order_page import customer_file_key


def test_picture_copies_run_four_at_a_time_and_once_per_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No database: 10 pictures over two revisions, three of them one blob twice."""
    running = peak = 0
    calls: list[str] = []
    held: list[tuple[str, str]] = []

    async def picture_url(state: Any, content: Any, attachment: Any, key: str) -> str:
        nonlocal running, peak
        calls.append(key)
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return f"url-{key}"

    async def hold(state: Any, key: str, attachment_id: str) -> None:
        held.append((key, attachment_id))

    monkeypatch.setattr(order_page, "_picture_url", picture_url)
    monkeypatch.setattr(order_page, "_hold", hold)

    def att(n: int, sha: str) -> Any:
        return SimpleNamespace(id=f"a{n}", sha256=sha)

    sent = datetime.now(UTC)
    revisions = [
        SimpleNamespace(id="r1", sent_at=sent),
        SimpleNamespace(id="r2", sent_at=sent),
        SimpleNamespace(id="r3", sent_at=None),
    ]
    pictures = {
        "r1": [(att(i, f"s{i}"), f"k{i}") for i in range(6)],
        "r2": [
            (att(6, "s0"), "k0"),
            (att(7, "s1"), "k1"),
            (att(8, "s6"), "k6"),
            (att(9, "s7"), "k7"),
        ],
        "r3": [(att(10, "s9"), "k9")],
    }
    rows = SimpleNamespace(revisions={"i": revisions}, pictures=pictures)
    got = asyncio.run(
        order_page._picture_urls(None, object(), SimpleNamespace(id="i"), rows)  # type: ignore[arg-type]
    )
    assert sorted(calls) == [f"k{i}" for i in range(8)]
    assert got == {f"k{i}": f"url-k{i}" for i in range(8)}
    assert peak == 4
    assert sorted(held) == sorted(
        [(customer_file_key("s0"), "a6"), (customer_file_key("s1"), "a7")]
    )
