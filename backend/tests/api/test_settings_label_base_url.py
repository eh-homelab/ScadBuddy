"""#6955: `filament_label_base_url`, the address a printed location label opens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scadbuddy.filament.labels import label_base_url

URL = "/api/v1/settings"


def test_saves_an_https_origin(client: TestClient) -> None:
    assert client.get(URL).json()["filament_label_base_url"] is None
    saved = client.put(URL, json={"filament_label_base_url": "https://sb.example.com"})
    assert saved.status_code == 200, saved.text
    body = client.get(URL).json()
    assert body["filament_label_base_url"] == "https://sb.example.com"
    assert body["applies"]["filament_label_base_url"] == "live"
    cleared = client.put(URL, json={"filament_label_base_url": None})
    assert cleared.status_code == 200, cleared.text
    assert client.get(URL).json()["filament_label_base_url"] is None


@pytest.mark.parametrize(
    "value",
    ["https://sb.example.com/x", "http://sb.example.com", "https://sb.example.com/"],
)
def test_refuses_a_path_or_http(client: TestClient, value: str) -> None:
    assert client.put(URL, json={"filament_label_base_url": value}).status_code == 422


def test_refuses_over_64_characters(client: TestClient) -> None:
    long = "https://" + "a" * 50 + ".example.com"
    assert len(long) > 64
    assert client.put(URL, json={"filament_label_base_url": long}).status_code == 422


def test_falls_back_to_public_url() -> None:
    read = {"filament_label_base_url": None, "public_url": "https://p.example"}
    assert label_base_url(read) == "https://p.example"


def test_prefers_the_setting_and_ends_with_none() -> None:
    both = {"filament_label_base_url": "https://l.example", "public_url": "https://p.example"}
    assert label_base_url(both) == "https://l.example"
    assert label_base_url({"filament_label_base_url": None, "public_url": None}) is None
