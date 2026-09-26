from __future__ import annotations

import httpx
import pytest
import respx

from scadbuddy.library.url_import import (
    ImportRefusedError,
    SourceUnreachableError,
    fetch_model,
)

RAW_URL = "https://raw.githubusercontent.com/someone/models/main/Gridfinity%20Bin.scad"
SOURCE = "width = 10;\ncube(width);\n"
LIMIT = 1024


@respx.mock
async def test_a_raw_scad_url_yields_its_source_named_after_the_file() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    imported = await fetch_model(RAW_URL, limit=LIMIT)

    assert imported.source == SOURCE
    assert imported.name == "Gridfinity Bin"
    assert imported.origin_url == RAW_URL


@respx.mock
async def test_a_url_with_no_file_name_is_named_after_its_host() -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, text=SOURCE))

    imported = await fetch_model("https://example.com/", limit=LIMIT)

    assert imported.name == "example.com"


@pytest.mark.parametrize(
    "url",
    ["http://example.com/model.scad", "ftp://example.com/model.scad", "file:///etc/passwd"],
)
async def test_anything_but_https_is_refused_before_a_request_is_made(url: str) -> None:
    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError) as caught:
        await fetch_model(url, limit=LIMIT)
    assert "https" in str(caught.value)
    assert not mock.calls


async def test_a_string_that_is_not_a_url_is_refused() -> None:
    with pytest.raises(ImportRefusedError):
        await fetch_model("not a url", limit=LIMIT)


@respx.mock
async def test_a_redirect_to_plain_http_is_not_followed() -> None:
    respx.get("https://example.com/model.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "http://example.com/model.scad"})
    )
    downgraded = respx.get("http://example.com/model.scad")

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://example.com/model.scad", limit=LIMIT)

    assert "https" in str(caught.value)
    assert not downgraded.called


@respx.mock
async def test_an_https_redirect_is_followed_and_the_pasted_url_is_the_origin() -> None:
    respx.get("https://example.com/latest.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/v2.scad"})
    )
    respx.get("https://cdn.example.com/v2.scad").mock(return_value=httpx.Response(200, text=SOURCE))

    imported = await fetch_model("https://example.com/latest.scad", limit=LIMIT)

    assert imported.source == SOURCE
    assert imported.origin_url == "https://example.com/latest.scad"


@respx.mock
async def test_a_body_over_the_limit_is_refused() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="x" * (LIMIT + 1)))

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert str(LIMIT) in str(caught.value)


@respx.mock
async def test_an_html_page_is_refused_rather_than_parse_checked() -> None:
    respx.get("https://github.com/someone/models/blob/main/bin.scad").mock(
        return_value=httpx.Response(
            200, text="<!doctype html><html></html>", headers={"Content-Type": "text/html"}
        )
    )

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://github.com/someone/models/blob/main/bin.scad", limit=LIMIT)

    assert "raw" in str(caught.value)


@respx.mock
async def test_a_binary_body_is_refused() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, content=b"PK\x03\x04\x00\x00"))

    with pytest.raises(ImportRefusedError):
        await fetch_model(RAW_URL, limit=LIMIT)


@respx.mock
async def test_an_upstream_error_status_is_unreachable_not_refused() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(SourceUnreachableError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert "404" in str(caught.value)
    assert not caught.value.timed_out


@respx.mock
async def test_a_connection_failure_is_unreachable() -> None:
    respx.get(RAW_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(SourceUnreachableError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert not caught.value.timed_out


@respx.mock
async def test_a_timeout_says_it_was_one() -> None:
    respx.get(RAW_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(SourceUnreachableError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert caught.value.timed_out


@pytest.mark.parametrize(
    "url",
    [
        "https://makerworld.com/en/models/1398039-parametric-customizable-keychain-openscad",
        "https://www.makerworld.com/models/1398039",
    ],
)
async def test_a_makerworld_model_page_is_refused_with_the_way_round_it(url: str) -> None:
    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError) as caught:
        await fetch_model(url, limit=LIMIT)
    message = str(caught.value)
    assert "MakerWorld" in message
    assert "Upload" in message
    assert not mock.calls
