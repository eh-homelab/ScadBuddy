"""The storefront's origin (#4960): one validator, shared by the API's settings and the
edge's, so both refuse the same values."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.core import urls
from scadbuddy.core.settings import BOOTSTRAP_FIELDS, Settings
from scadbuddy.storefront import settings as edge_settings
from tests.conftest import UNUSED_DATABASE_URL

TEMPORAL = "temporal.invalid:7233"

BAD = ["http://shop.example.com", "https://shop.example.com/pay", "https://", "shop.example.com",
       "https://shop.example.com?x=1", "https://shop.example.com#f"]  # fmt: skip


def test_an_https_origin_is_kept() -> None:
    assert urls.https_origin("https://shop.example.com", "X") == "https://shop.example.com"


@pytest.mark.parametrize("url", BAD)
def test_anything_else_is_refused_naming_the_variable(url: str) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_X"):
        urls.https_origin(url, "SCADBUDDY_X")


def test_the_api_reads_the_storefront_origin_from_its_environment_only() -> None:
    assert "storefront_public_url" in BOOTSTRAP_FIELDS
    made = Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=TEMPORAL,
        storefront_public_url="https://shop.example.com",
    )
    assert made.storefront_public_url == "https://shop.example.com"
    assert (
        Settings(database_url=UNUSED_DATABASE_URL, temporal_address=TEMPORAL).storefront_public_url
        is None
    )


@pytest.mark.parametrize("url", BAD)
def test_the_api_refuses_what_the_edge_refuses(url: str) -> None:
    with pytest.raises(ValidationError, match="SCADBUDDY_STOREFRONT_PUBLIC_URL"):
        Settings(
            database_url=UNUSED_DATABASE_URL, temporal_address=TEMPORAL, storefront_public_url=url
        )


def test_the_edge_uses_the_shared_validator() -> None:
    assert getattr(edge_settings, "https_origin") is urls.https_origin  # noqa: B009
