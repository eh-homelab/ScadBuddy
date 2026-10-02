"""`fetch_file`'s allowlist is checked before any name is resolved (#844)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scadbuddy.library import url_import
from scadbuddy.library.asset_fetch import AssetFetchRefusedError, fetch_file
from tests.conftest import PUBLIC_ADDRESS

DOMAINS = ("openmoji.org",)


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every host the fetch looked up, in order."""
    hosts: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        hosts.append(host)
        return [PUBLIC_ADDRESS]

    monkeypatch.setattr(url_import, "resolve_host", resolve)
    return hosts


async def test_a_host_off_the_allowlist_is_never_resolved(resolved: list[str]) -> None:
    with pytest.raises(AssetFetchRefusedError):
        await fetch_file("https://example.com/a.svg", domains=DOMAINS, limit=1024)
    assert resolved == []


async def test_a_redirect_off_the_allowlist_is_never_resolved(resolved: list[str]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://openmoji.org/a.svg").mock(
            return_value=httpx.Response(302, headers={"Location": "https://example.com/a.svg"})
        )
        with pytest.raises(AssetFetchRefusedError, match=r"example\.com"):
            await fetch_file("https://openmoji.org/a.svg", domains=DOMAINS, limit=1024)
    assert "example.com" not in resolved
