"""#6955 — the label base falls back to the origin of ``public_url``, https only."""

import pytest

from scadbuddy.filament.labels import label_base_url


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        ({"public_url": "https://h.example/p/"}, "https://h.example"),
        ({"public_url": "https://h.example:8443/p"}, "https://h.example:8443"),
        ({"public_url": "http://h.example"}, None),
        ({"public_url": ""}, None),
        ({"public_url": None}, None),
        ({"filament_label_base_url": "", "public_url": "https://h.example/"}, "https://h.example"),
        (
            {"filament_label_base_url": "https://a.example", "public_url": "https://h.example"},
            "https://a.example",
        ),
    ],
)
def test_label_base_url(read: dict[str, str | None], expected: str | None) -> None:
    assert label_base_url(read) == expected
