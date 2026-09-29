from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import get_fonts
from scadbuddy.api.params import InstalledFamilies, require_installed_fonts
from scadbuddy.core.problems import ApiError
from scadbuddy.library.fonts import (
    MANIFEST_NAME,
    FontFamily,
    FontService,
    list_fonts,
    normalise_family,
    parse_fc_list,
)
from scadbuddy.library.googlefonts import (
    CatalogueFont,
    FamilyFiles,
    FontCatalogue,
    FontFile,
    FontVariant,
    GoogleFontsError,
)
from scadbuddy.render.schema import CustomizerSchema, Parameter

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

    async def fetch_family_files(self, family: str) -> FamilyFiles:
        if self.download_fails:
            raise GoogleFontsError("the repository said no")
        return FamilyFiles(
            family=family,
            directory="ofl",
            licence="OFL",
            files=[
                FontFile(
                    variant=FontVariant(),
                    filename=f"{family.replace(' ', '')}-Regular.ttf",
                    url=f"https://raw/ofl/x/{family}.ttf",
                )
            ],
        )

    async def fetch_file(self, url: str) -> bytes:
        return b"\x00\x01\x00\x00"

    async def fetch_licence(self, files: FamilyFiles) -> tuple[str, str] | None:
        return ("OFL.txt", "Copyright")


class FakeBackedService(FontService):
    """Real service, stubbed client, and a fontconfig answer the test controls.

    ``installed``, ``renderable`` and ``resolvable`` are overridden because the machine
    running the tests has its own fonts, and "which families are installed" is exactly
    what several of these assert on.
    """

    client: FakeClient  # type: ignore[assignment]

    def __init__(self, data_dir: Path, *, client: FakeClient) -> None:
        super().__init__(data_dir, client=client)  # type: ignore[arg-type]
        self.pretend_installed: list[FontFamily] = []

    def installed(self) -> list[FontFamily]:
        return list(self.pretend_installed)

    def renderable(self) -> list[FontFamily]:
        return list(self.pretend_installed)

    def refresh_cache(self) -> None:
        # What a working fc-cache does, unless the test says fontconfig ignores them.
        if self.downloads_resolve:
            self.pretend_installed = [
                *self.pretend_installed,
                *(
                    FontFamily(
                        family=json.loads(m.read_text(encoding="utf-8"))["family"],
                        styles=["Regular"],
                    )
                    for m in self.root.glob(f"*/{MANIFEST_NAME}")
                ),
            ]

    def resolvable(self) -> set[str] | None:
        return {normalise_family(family.family) for family in self.pretend_installed}

    downloads_resolve = True


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


def test_an_install_fontconfig_then_does_not_resolve_is_a_500_naming_the_files(
    client: TestClient, fonts: FakeBackedService
) -> None:
    """#253: not a success that leaves every render of the family in DejaVu Sans."""
    fonts.downloads_resolve = False
    response = client.post("/api/v1/fonts/install", json={"family": "Pacifico"})
    assert response.status_code == 500
    body = response.json()
    assert "does not resolve" in body["detail"]
    assert body["family"] == "Pacifico"
    assert body["files"] == ["Pacifico-Regular.ttf"]


# ── a `// font` value names an installed family (#253) ───────────────────────────


class Resolving(FontService):
    def __init__(self, families: set[str] | None) -> None:
        super().__init__(Path("/nonexistent"))
        self.families = families
        self.asked = 0

    def resolvable(self) -> set[str] | None:
        self.asked += 1
        return None if self.families is None else {normalise_family(f) for f in self.families}


FONT_SCHEMA = CustomizerSchema(
    parameters=[
        Parameter(name="font", type="font", initial="Lobster Two:style=Bold"),
        Parameter(name="label", type="string", initial="hi"),
    ]
)


async def test_a_font_value_whose_family_is_not_installed_is_a_422() -> None:
    fonts = Resolving({"DejaVu Sans", "Lobster Two"})
    with pytest.raises(ApiError) as raised:
        await require_installed_fonts(FONT_SCHEMA, {"font": "Pacifico:style=Regular"}, fonts)
    assert raised.value.status == 422
    assert "'Pacifico'" in raised.value.detail
    assert "default font" in raised.value.detail
    assert raised.value.extensions == {"parameters": ["font"], "families": ["Pacifico"]}


async def test_an_installed_family_passes_whatever_its_case_and_spacing() -> None:
    fonts = Resolving({"DejaVu Sans"})
    await require_installed_fonts(FONT_SCHEMA, {"font": "dejavusans:style=Bold"}, fonts)


async def test_the_templates_own_default_and_the_default_font_are_not_judged() -> None:
    fonts = Resolving(set())
    await require_installed_fonts(
        FONT_SCHEMA, {"font": "Lobster Two:style=Bold", "label": "Pacifico"}, fonts
    )
    await require_installed_fonts(FONT_SCHEMA, {"font": ""}, fonts)
    await require_installed_fonts(FONT_SCHEMA, {"font": ":style=Bold"}, fonts)
    assert fonts.asked == 0


async def test_a_bare_dash_is_explained() -> None:
    with pytest.raises(ApiError) as raised:
        await require_installed_fonts(FONT_SCHEMA, {"font": "Unifont-JP"}, Resolving(set()))
    assert "'Unifont'" in raised.value.detail
    assert "\\-" in raised.value.detail


async def test_one_ask_of_fontconfig_serves_every_value_judged_against_it() -> None:
    """require_valid_presets checks a template's presets with one InstalledFamilies,
    so fc-list runs once per request, not once per preset (#740 review)."""
    fonts = Resolving({"DejaVu Sans"})
    installed = InstalledFamilies(fonts)
    for _ in range(3):
        await require_installed_fonts(FONT_SCHEMA, {"font": "DejaVu Sans"}, installed)
    assert fonts.asked == 1


async def test_the_dash_hint_is_only_for_the_family_the_dash_cut() -> None:
    """In a fallback list the dash cuts the last family; a missing earlier one gets no
    hint to escape a dash it does not have (#740 review)."""
    with pytest.raises(ApiError) as raised:
        await require_installed_fonts(
            FONT_SCHEMA, {"font": "Arial,Unifont-JP"}, Resolving({"Unifont"})
        )
    assert "'Arial'" in raised.value.detail
    assert "\\-" not in raised.value.detail


def test_a_render_or_preset_naming_a_family_that_is_not_installed_is_refused(
    app: FastAPI, client: TestClient
) -> None:
    """The routes an agent sets a font through: a render and a saved preset (#253).
    The fake openscad exports `label` as a string, which `// font` overlays."""
    app.dependency_overrides[get_fonts] = lambda: Resolving({"DejaVu Sans"})
    source = 'width = 10;\nlabel = "DejaVu Sans"; // font\n'
    assert client.post("/api/v1/models", json={"name": "sign", "source": source}).status_code == 201

    render = client.post("/api/v1/models/sign/render", json={"params": {"label": "Pacifico"}})
    preset = client.post(
        "/api/v1/models/sign/presets", json={"name": "curly", "params": {"label": "Pacifico"}}
    )

    for response in (render, preset):
        assert response.status_code == 422, response.text
        assert response.json()["families"] == ["Pacifico"]
    fine = client.post("/api/v1/models/sign/render", json={"params": {"label": "dejavu sans"}})
    assert fine.status_code == 202, fine.text


async def test_without_fontconfig_nothing_is_refused() -> None:
    await require_installed_fonts(FONT_SCHEMA, {"font": "Pacifico"}, Resolving(None))
