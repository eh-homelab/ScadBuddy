"""The customer link rules and the handler registry (#6133, spec 2026-11-13 §9)."""

from __future__ import annotations

import pytest

from scadbuddy.orders.customer_links import (
    MAX_LINK_CHARS,
    LinkInvalidError,
    checked_link,
    link_type,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/thing",
        "http://example.com",
        "https://shop.example.co.uk/magnets?size=6mm#reviews",
    ],
)
def test_any_http_link_is_taken(url: str) -> None:
    assert checked_link(url) == url


def test_the_host_is_lower_cased_and_made_ascii() -> None:
    assert checked_link("https://EXAMPLE.com/A") == "https://example.com/A"
    assert checked_link("https://bücher.example/x") == "https://xn--bcher-kva.example/x"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "data:text/html,hi",
        "https://",
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https://example.com/a b",
        "https://example.com/\x00",
        "https://example.com/\n",
        " https://example.com/",
        "https://" + "a" * MAX_LINK_CHARS + ".com",
        "//example.com/x",
        "https://exa mple.com/",
    ],
)
def test_a_link_that_breaks_a_rule_is_refused(url: str) -> None:
    with pytest.raises(LinkInvalidError):
        checked_link(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://makerworld.com/en/models/123456", ("makerworld", "123456")),
        ("https://makerworld.com/models/123456-cute-frog", ("makerworld", "123456")),
        ("https://www.makerworld.com/zh-hans/models/42/", ("makerworld", "42")),
        (
            "https://makerworld.com/en/models/123456?from=search#profileId-9",
            ("makerworld", "123456"),
        ),
        ("https://www.printables.com/model/98765-phone-stand", ("printables", "98765")),
        ("https://printables.com/de/model/98765", ("printables", "98765")),
        ("https://www.thingiverse.com/thing:4321", ("thingiverse", "4321")),
        ("https://thingiverse.com/thing:4321/", ("thingiverse", "4321")),
    ],
)
def test_a_print_link_is_typed_by_its_handler(url: str, expected: tuple[str, str]) -> None:
    assert link_type(checked_link(url), "print") == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://makerworld.com/en/models/123456",
        "https://makerworld.com:8443/en/models/123456",
        "https://makerworld.com.cn/en/models/123456",
        "https://evil-makerworld.com/en/models/123456",
        "https://makerworld.com.evil.example/en/models/123456",
        "https://makerworld.com/en/models/abc",
        "https://makerworld.com/en/models/123/extra",
        "https://makerworld.com/en/collections/123",
        "https://www.thingiverse.com/thing:12/files",
        "https://example.com/models/1",
    ],
)
def test_a_link_no_handler_matches_is_manual(url: str) -> None:
    assert link_type(checked_link(url), "print") == ("manual", None)


def test_a_purchase_link_is_always_manual() -> None:
    url = checked_link("https://makerworld.com/en/models/123456")
    assert link_type(url, "purchase") == ("manual", None)
