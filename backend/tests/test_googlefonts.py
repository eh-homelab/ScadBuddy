from __future__ import annotations

import json

import httpx
import pytest
import respx

from scadbuddy.library.googlefonts import (
    CSS_URL,
    DEVELOPER_API_URL,
    LEGACY_USER_AGENT,
    LICENCE_BASE_URL,
    METADATA_URL,
    CatalogueFont,
    FontVariant,
    GoogleFontsClient,
    GoogleFontsError,
    css2_family_query,
    licence_slug,
    parse_css_faces,
    parse_developer_api,
    parse_metadata,
    parse_variant,
    strip_xssi,
    style_name,
)

DEVELOPER_PAYLOAD = {
    "items": [
        {
            "family": "Roboto",
            "category": "sans-serif",
            "variants": ["100", "regular", "italic", "700italic"],
            "files": {
                "100": "https://fonts.gstatic.com/s/roboto/v1/thin.ttf",
                "regular": "https://fonts.gstatic.com/s/roboto/v1/regular.ttf",
                "italic": "https://fonts.gstatic.com/s/roboto/v1/italic.ttf",
                "700italic": "https://fonts.gstatic.com/s/roboto/v1/bolditalic.ttf",
            },
        },
        {
            "family": "Pacifico",
            "category": "handwriting",
            "variants": ["regular"],
            "files": {"regular": "https://fonts.gstatic.com/s/pacifico/v22/pacifico.ttf"},
        },
    ]
}

METADATA_PAYLOAD = {
    "familyMetadataList": [
        {
            "family": "Pacifico",
            "category": "Handwriting",
            "popularity": 42,
            "fonts": {"400": {}},
        },
        {
            "family": "Roboto",
            "category": "Sans Serif",
            "popularity": 1,
            "fonts": {"100": {}, "400": {}, "400i": {}, "700i": {}},
        },
    ]
}

CSS_BODY = """\
@font-face {
  font-family: 'Pacifico';
  font-style: normal;
  font-weight: 400;
  src: url(https://fonts.gstatic.com/s/pacifico/v22/FwZY7-Q.ttf) format('truetype');
}
@font-face {
  font-family: 'Pacifico';
  font-style: italic;
  font-weight: 700;
  src: url(https://fonts.gstatic.com/s/pacifico/v22/BoldItalic.ttf) format('truetype');
}
"""


def test_variants_parse_from_both_spellings() -> None:
    assert parse_variant("regular") == FontVariant(weight=400, italic=False)
    assert parse_variant("italic") == FontVariant(weight=400, italic=True)
    assert parse_variant("700italic") == FontVariant(weight=700, italic=True)
    assert parse_variant("700i") == FontVariant(weight=700, italic=True)
    assert parse_variant("300") == FontVariant(weight=300, italic=False)


def test_an_unrecognised_variant_is_an_error() -> None:
    with pytest.raises(ValueError):
        parse_variant("ultrabold")


def test_style_names_are_the_ones_openscad_is_given() -> None:
    assert style_name(400, False) == "Regular"
    assert style_name(400, True) == "Italic"
    assert style_name(700, False) == "Bold"
    assert style_name(300, True) == "Light Italic"
    # An off-scale weight is passed through rather than guessed at.
    assert style_name(450, False) == "450"


def test_the_developer_api_rank_is_the_row_order() -> None:
    fonts = parse_developer_api(DEVELOPER_PAYLOAD)
    assert [font.family for font in fonts] == ["Roboto", "Pacifico"]
    assert [font.popularity for font in fonts] == [1, 2]
    assert fonts[0].files["regular"].endswith("regular.ttf")


def test_the_developer_api_variants_are_deduplicated_and_ordered() -> None:
    roboto = parse_developer_api(DEVELOPER_PAYLOAD)[0]
    assert [(v.weight, v.italic) for v in roboto.variants] == [
        (100, False),
        (400, False),
        (400, True),
        (700, True),
    ]


def test_metadata_categories_are_normalised_to_the_developer_api_spelling() -> None:
    fonts = parse_metadata(METADATA_PAYLOAD)
    assert {font.family: font.category for font in fonts} == {
        "Roboto": "sans-serif",
        "Pacifico": "handwriting",
    }


def test_metadata_rows_come_back_most_popular_first() -> None:
    assert [font.family for font in parse_metadata(METADATA_PAYLOAD)] == ["Roboto", "Pacifico"]


def test_metadata_carries_no_file_urls() -> None:
    assert all(font.files == {} for font in parse_metadata(METADATA_PAYLOAD))


def test_the_xssi_guard_is_stripped() -> None:
    assert strip_xssi(')]}\'\n{"a": 1}') == '{"a": 1}'
    assert strip_xssi('{"a": 1}') == '{"a": 1}'


def test_the_css_query_lists_the_axes_in_ascending_order() -> None:
    assert css2_family_query("Pacifico", [FontVariant()]) == "Pacifico:wght@400"
    assert (
        css2_family_query(
            "Roboto",
            [FontVariant(weight=700, italic=True), FontVariant(weight=400)],
        )
        == "Roboto:ital,wght@0,400;1,700"
    )


def test_css_faces_map_to_variant_keys() -> None:
    assert parse_css_faces(CSS_BODY) == {
        "regular": "https://fonts.gstatic.com/s/pacifico/v22/FwZY7-Q.ttf",
        "700italic": "https://fonts.gstatic.com/s/pacifico/v22/BoldItalic.ttf",
    }


def test_a_woff2_face_is_not_a_usable_file() -> None:
    woff2 = CSS_BODY.replace(".ttf", ".woff2").replace("truetype", "woff2")
    assert parse_css_faces(woff2) == {}


def test_the_licence_slug_matches_the_repository_layout() -> None:
    assert licence_slug("Noto Sans JP") == "notosansjp"
    assert licence_slug("Pacifico") == "pacifico"


@respx.mock
async def test_a_key_selects_the_developer_api_and_never_the_metadata() -> None:
    route = respx.get(DEVELOPER_API_URL).mock(
        return_value=httpx.Response(200, json=DEVELOPER_PAYLOAD)
    )
    metadata = respx.get(METADATA_URL)

    catalogue = await GoogleFontsClient("secret-key").fetch_catalogue()

    assert catalogue.source == "developer-api"
    assert route.called
    assert not metadata.called
    assert route.calls[0].request.url.params["key"] == "secret-key"


@respx.mock
async def test_without_a_key_the_keyless_metadata_answers() -> None:
    route = respx.get(METADATA_URL).mock(
        return_value=httpx.Response(200, text=")]}'\n" + json.dumps(METADATA_PAYLOAD))
    )

    catalogue = await GoogleFontsClient().fetch_catalogue()

    assert catalogue.source == "google-fonts-metadata"
    assert route.called
    assert catalogue.find("pacifico") is not None


@respx.mock
async def test_an_empty_catalogue_is_an_error_rather_than_an_empty_list() -> None:
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, text=")]}'\n{}"))
    with pytest.raises(GoogleFontsError):
        await GoogleFontsClient().fetch_catalogue()


@respx.mock
async def test_a_non_2xx_catalogue_response_is_an_error() -> None:
    respx.get(METADATA_URL).mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(GoogleFontsError):
        await GoogleFontsClient().fetch_catalogue()


@respx.mock
async def test_files_come_straight_off_the_developer_api_row_with_no_second_call() -> None:
    css = respx.get(CSS_URL)
    font = parse_developer_api(DEVELOPER_PAYLOAD)[1]

    assert await GoogleFontsClient("k").resolve_files(font) == {
        "regular": "https://fonts.gstatic.com/s/pacifico/v22/pacifico.ttf"
    }
    assert not css.called


@respx.mock
async def test_without_file_urls_the_css_endpoint_is_asked_with_a_legacy_user_agent() -> None:
    route = respx.get(CSS_URL).mock(return_value=httpx.Response(200, text=CSS_BODY))
    font = CatalogueFont(family="Pacifico", variants=[FontVariant()])

    files = await GoogleFontsClient().resolve_files(font)

    assert set(files) == {"regular", "700italic"}
    assert route.calls[0].request.headers["User-Agent"] == LEGACY_USER_AGENT
    assert route.calls[0].request.url.params["family"] == "Pacifico:wght@400"


@respx.mock
async def test_css_that_yields_no_truetype_is_reported_rather_than_returning_nothing() -> None:
    respx.get(CSS_URL).mock(return_value=httpx.Response(200, text="/* woff2 only */"))
    with pytest.raises(GoogleFontsError):
        await GoogleFontsClient().resolve_files(CatalogueFont(family="Pacifico"))


@respx.mock
async def test_the_licence_falls_through_the_three_licence_directories() -> None:
    respx.get(f"{LICENCE_BASE_URL}/ofl/pacifico/OFL.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{LICENCE_BASE_URL}/apache/pacifico/LICENSE.txt").mock(
        return_value=httpx.Response(200, text="Apache License")
    )

    assert await GoogleFontsClient().fetch_licence("Pacifico") == ("LICENSE.txt", "Apache License")


@respx.mock
async def test_no_licence_anywhere_is_a_none_rather_than_an_error() -> None:
    respx.get(url__startswith=LICENCE_BASE_URL).mock(return_value=httpx.Response(404))
    assert await GoogleFontsClient().fetch_licence("Pacifico") is None
