from __future__ import annotations

import json

import httpx
import pytest
import respx

from scadbuddy.library.googlefonts import (
    DEVELOPER_API_URL,
    METADATA_URL,
    REPO_BASE_URL,
    FamilyFiles,
    FontVariant,
    GoogleFontsClient,
    GoogleFontsError,
    licence_slug,
    parse_developer_api,
    parse_family_metadata,
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
        },
        {
            "family": "Pacifico",
            "category": "handwriting",
            "variants": ["regular"],
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

# Trimmed from the real ofl/lobstertwo/METADATA.pb.
METADATA_PB_BODY = """\
name: "Lobster Two"
license: "OFL"
category: "DISPLAY"
fonts {
  name: "Lobster Two"
  style: "normal"
  weight: 400
  filename: "LobsterTwo-Regular.ttf"
}
fonts {
  name: "Lobster Two"
  style: "italic"
  weight: 700
  filename: "LobsterTwo-BoldItalic.ttf"
}
source {
  repository_url: "https://github.com/googlefonts/lobstertwo"
}
"""

# A variable family lists one file against every named instance.
VARIABLE_PB_BODY = """\
name: "Noto Sans"
license: "OFL"
fonts {
  style: "normal"
  weight: 400
  filename: "NotoSans[wdth,wght].ttf"
}
fonts {
  style: "normal"
  weight: 700
  filename: "NotoSans[wdth,wght].ttf"
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


def test_the_xssi_guard_is_stripped() -> None:
    assert strip_xssi(')]}\'\n{"a": 1}') == '{"a": 1}'
    assert strip_xssi('{"a": 1}') == '{"a": 1}'


def test_metadata_pb_yields_one_file_per_face_with_its_weight_and_slant() -> None:
    parsed = parse_family_metadata(
        METADATA_PB_BODY, family="Lobster Two", directory="ofl", base_url="https://raw"
    )
    assert parsed.licence == "OFL"
    assert [(f.filename, f.variant.weight, f.variant.italic) for f in parsed.files] == [
        ("LobsterTwo-Regular.ttf", 400, False),
        ("LobsterTwo-BoldItalic.ttf", 700, True),
    ]
    assert parsed.files[0].url == "https://raw/ofl/lobstertwo/LobsterTwo-Regular.ttf"


def test_a_variable_family_is_downloaded_once_not_once_per_named_instance() -> None:
    parsed = parse_family_metadata(
        VARIABLE_PB_BODY, family="Noto Sans", directory="ofl", base_url="https://raw"
    )
    assert [f.filename for f in parsed.files] == ["NotoSans[wdth,wght].ttf"]


def test_a_variable_filename_is_escaped_for_the_url_but_the_comma_is_not() -> None:
    """Measured against raw.githubusercontent.com: it accepts %5B/%5D and a bare comma."""
    parsed = parse_family_metadata(
        VARIABLE_PB_BODY, family="Noto Sans", directory="ofl", base_url="https://raw"
    )
    assert parsed.files[0].url == "https://raw/ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf"


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
async def test_the_repository_is_probed_for_the_licence_directory_holding_the_family() -> None:
    respx.get(f"{REPO_BASE_URL}/ofl/lobstertwo/METADATA.pb").mock(return_value=httpx.Response(404))
    respx.get(f"{REPO_BASE_URL}/apache/lobstertwo/METADATA.pb").mock(
        return_value=httpx.Response(200, text=METADATA_PB_BODY)
    )

    found = await GoogleFontsClient().fetch_family_files("Lobster Two")

    assert found.directory == "apache"
    assert [file.filename for file in found.files] == [
        "LobsterTwo-Regular.ttf",
        "LobsterTwo-BoldItalic.ttf",
    ]


@respx.mock
async def test_a_family_the_repository_does_not_carry_is_an_error() -> None:
    respx.get(url__startswith=REPO_BASE_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(GoogleFontsError, match="google/fonts"):
        await GoogleFontsClient().fetch_family_files("Pacifico")


@respx.mock
async def test_a_metadata_file_naming_no_fonts_is_an_error() -> None:
    respx.get(f"{REPO_BASE_URL}/ofl/pacifico/METADATA.pb").mock(
        return_value=httpx.Response(200, text='name: "Pacifico"\nlicense: "OFL"\n')
    )
    with pytest.raises(GoogleFontsError, match="no font files"):
        await GoogleFontsClient().fetch_family_files("Pacifico")


async def test_a_family_name_with_no_usable_slug_never_reaches_the_network() -> None:
    with pytest.raises(GoogleFontsError, match="directory name"):
        await GoogleFontsClient().fetch_family_files("!!!")


@respx.mock
async def test_the_licence_named_in_the_metadata_is_the_one_fetched() -> None:
    route = respx.get(f"{REPO_BASE_URL}/ufl/ubuntu/UFL.txt").mock(
        return_value=httpx.Response(200, text="Ubuntu Font Licence")
    )
    files = FamilyFiles(family="Ubuntu", directory="ufl", licence="UFL")

    assert await GoogleFontsClient().fetch_licence(files) == ("UFL.txt", "Ubuntu Font Licence")
    assert route.called


@respx.mock
async def test_an_unexpected_licence_name_falls_through_the_known_filenames() -> None:
    respx.get(f"{REPO_BASE_URL}/ofl/pacifico/OFL.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{REPO_BASE_URL}/ofl/pacifico/LICENSE.txt").mock(
        return_value=httpx.Response(200, text="Apache License")
    )
    files = FamilyFiles(family="Pacifico", directory="ofl", licence="")

    assert await GoogleFontsClient().fetch_licence(files) == ("LICENSE.txt", "Apache License")


@respx.mock
async def test_no_licence_anywhere_is_a_none_rather_than_an_error() -> None:
    respx.get(url__startswith=REPO_BASE_URL).mock(return_value=httpx.Response(404))
    files = FamilyFiles(family="Pacifico", directory="ofl", licence="OFL")
    assert await GoogleFontsClient().fetch_licence(files) is None
