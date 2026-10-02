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


@pytest.mark.usefixtures("resolved")
async def test_hosts_are_compared_as_the_ascii_name_that_is_connected_to() -> None:
    """A non-ASCII host is matched in its IDNA (punycode) form, the one that goes on
    the wire, so a punycode entry allows its Unicode spelling and a
    Unicode label under an allowed domain is still that domain's subdomain."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://xn--r8jz45g.com/a.svg").mock(
            return_value=httpx.Response(200, content=svg)
        )
        mock.get("https://xn--tda.openmoji.org/a.svg").mock(
            return_value=httpx.Response(200, content=svg)
        )
        idn = await fetch_file("https://例え.com/a.svg", domains=("xn--r8jz45g.com",), limit=1024)
        sub = await fetch_file("https://ü.openmoji.org/a.svg", domains=DOMAINS, limit=1024)
    assert (idn.data, sub.data) == (svg, svg)
    with pytest.raises(AssetFetchRefusedError):
        await fetch_file("https://例え.com/a.svg", domains=DOMAINS, limit=1024)
