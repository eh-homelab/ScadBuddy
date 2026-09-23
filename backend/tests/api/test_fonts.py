from __future__ import annotations

from fastapi.testclient import TestClient

from scadbuddy.library.fonts import list_fonts, parse_fc_list

FC_LIST_SAMPLE = """\
Lobster Two:style=Bold
Lobster Two:style=Regular
DejaVu Sans,DejaVu Sans Condensed:style=Condensed Bold,Bold
DejaVu Sans:style=Book

Noto Sans Arabic:style=Regular
"""


def test_the_first_family_and_style_of_each_line_are_kept() -> None:
    parsed = {family.family: family.styles for family in parse_fc_list(FC_LIST_SAMPLE)}
    assert parsed["Lobster Two"] == ["Bold", "Regular"]
    assert parsed["DejaVu Sans"] == ["Book", "Condensed Bold"]
    assert "DejaVu Sans Condensed" not in parsed
    assert parsed["Noto Sans Arabic"] == ["Regular"]


def test_families_come_back_sorted() -> None:
    families = [family.family for family in parse_fc_list(FC_LIST_SAMPLE)]
    assert families == sorted(families)


def test_a_line_without_a_style_still_yields_the_family() -> None:
    assert parse_fc_list("Some Font\n") == parse_fc_list("Some Font:style=\n")


def test_the_route_answers_with_whatever_is_installed(client: TestClient) -> None:
    response = client.get("/api/v1/fonts")
    assert response.status_code == 200
    assert response.json() == [family.model_dump() for family in list_fonts()]
