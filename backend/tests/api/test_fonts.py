from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import get_fonts
from scadbuddy.library.fonts import FontFamily, FontService, list_fonts, parse_fc_list
from scadbuddy.library.googlefonts import (
    CatalogueFont,
    FontCatalogue,
    FontVariant,
    GoogleFontsError,
)

CATALOGUE = "/api/v1/fonts/catalogue"

CATALOGUE_FONTS = [
    CatalogueFont(family="Roboto", category="sans-serif", variants=[FontVariant()], popularity=1),
    CatalogueFont(
        family="Pacifico", category="handwriting", variants=[FontVariant()], popularity=2
    ),
    CatalogueFont(
        family="Noto Sans", category="sans-serif", variants=[FontVariant()], popularity=3
    ),
    CatalogueFont(family="Sansita", category="serif", variants=[FontVariant()], popularity=9),
]


class FakeClient:
    """The Google Fonts half, stubbed: these tests are about the routes, not the wire."""

    def __init__(self) -> None:
        self.fail = False
        self.download_fails = False

    async def fetch_catalogue(self) -> FontCatalogue:
        if self.fail:
            raise GoogleFontsError("no network")
        return FontCatalogue(
            source="google-fonts-metadata",
            fetched_at=datetime.now(UTC),
            fonts=list(CATALOGUE_FONTS),
        )

    async def resolve_files(self, font: CatalogueFont) -> dict[str, str]:
        if self.download_fails:
            raise GoogleFontsError("gstatic said no")
        return {"regular": f"https://x/{font.family}.ttf"}

    async def fetch_file(self, url: str) -> bytes:
        return b"\x00\x01\x00\x00"

    async def fetch_licence(self, family: str) -> tuple[str, str] | None:
        return ("OFL.txt", "Copyright")


class FakeBackedService(FontService):
    """Real service, stubbed client, and a fontconfig answer the test controls.

    ``installed`` is overridden because the machine running the tests has its own fonts,
    and "which families are installed" is exactly what several of these assert on.
    """

    client: FakeClient  # type: ignore[assignment]

    def __init__(self, data_dir: Path, *, client: FakeClient) -> None:
        super().__init__(data_dir, client=client)  # type: ignore[arg-type]
        self.pretend_installed: list[FontFamily] = []

    def installed(self) -> list[FontFamily]:
        return list(self.pretend_installed)

    def refresh_cache(self) -> None:
        return None


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


# ── catalogue and install ────────────────────────────────────────────────────────


@pytest.fixture
def fonts(app: FastAPI, data_dir: Path) -> FakeBackedService:
    """The real FontService with a stubbed Google Fonts client bolted underneath it."""
    service = FakeBackedService(data_dir, client=FakeClient())
    app.dependency_overrides[get_fonts] = lambda: service
    return service


def test_the_catalogue_reports_which_source_answered(
    client: TestClient, fonts: FakeBackedService
) -> None:
    body = client.get("/api/v1/fonts/catalogue").json()
    assert body["source"] == "google-fonts-metadata"
    assert [font["family"] for font in body["fonts"]] == [
        "Roboto",
        "Pacifico",
        "Noto Sans",
        "Sansita",
    ]


def test_a_search_puts_prefix_matches_first(client: TestClient, fonts: FakeBackedService) -> None:
    body = client.get(CATALOGUE, params={"q": "sans"}).json()
    # "Noto Sans" is the more popular of the two, but only "Sansita" starts with it.
    assert [font["family"] for font in body["fonts"]] == ["Sansita", "Noto Sans"]


def test_a_category_chip_filters(client: TestClient, fonts: FakeBackedService) -> None:
    body = client.get("/api/v1/fonts/catalogue", params={"category": "handwriting"}).json()
    assert [font["family"] for font in body["fonts"]] == ["Pacifico"]


def test_the_limit_trims_the_rows_but_total_counts_the_matches(
    client: TestClient, fonts: FakeBackedService
) -> None:
    body = client.get("/api/v1/fonts/catalogue", params={"limit": 1}).json()
    assert body["total"] == 4
    assert len(body["fonts"]) == 1


def test_a_row_fontconfig_already_resolves_is_marked_installed(
    client: TestClient, fonts: FakeBackedService
) -> None:
    fonts.pretend_installed = [FontFamily(family="Noto Sans", styles=["Regular"])]
    rows = {font["family"]: font["installed"] for font in client.get(CATALOGUE).json()["fonts"]}
    assert rows == {"Roboto": False, "Pacifico": False, "Noto Sans": True, "Sansita": False}


def test_an_unreachable_catalogue_is_a_503_problem(
    client: TestClient, fonts: FakeBackedService
) -> None:
    fonts.client.fail = True
    response = client.get(CATALOGUE)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")


def test_installing_returns_the_styles_to_write_into_the_font_string(
    client: TestClient, fonts: FakeBackedService
) -> None:
    response = client.post("/api/v1/fonts/install", json={"family": "Pacifico"})
    assert response.status_code == 200
    assert response.json()["family"] == "Pacifico"
    assert response.json()["styles"] == ["Regular"]


def test_installing_something_outside_the_catalogue_is_a_404(
    client: TestClient, fonts: FakeBackedService
) -> None:
    response = client.post("/api/v1/fonts/install", json={"family": "Comic Sans MS"})
    assert response.status_code == 404


def test_a_download_failure_is_reported_rather_than_left_to_the_render(
    client: TestClient, fonts: FakeBackedService
) -> None:
    fonts.client.download_fails = True
    response = client.post("/api/v1/fonts/install", json={"family": "Pacifico"})
    assert response.status_code == 502
    assert "could not be downloaded" in response.json()["detail"]


def test_an_empty_family_is_rejected_before_anything_is_fetched(
    client: TestClient, fonts: FakeBackedService
) -> None:
    assert client.post("/api/v1/fonts/install", json={"family": ""}).status_code == 422
