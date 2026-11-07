"""A fetched file's licence, known for the catalogues on the allowlist, and its
credit in the model's README (#959)."""

from __future__ import annotations

import pytest

from scadbuddy.library.asset_credits import credit_line, known_licence, with_credit
from scadbuddy.library.assets import AssetLicence


@pytest.mark.parametrize(
    ("url", "spdx", "author"),
    [
        ("https://api.iconify.design/mdi/unicorn.svg", "Apache-2.0", "Pictogrammers"),
        ("https://api.iconify.design/tabler/heart.svg", "MIT", "Paweł Kuna"),
        ("https://api.iconify.design/game-icons/unicorn.svg", "CC-BY-3.0", "GameIcons"),
        ("https://api.iconify.design/openmoji/unicorn.svg", "CC-BY-SA-4.0", "OpenMoji"),
        ("https://openmoji.org/data/color/svg/1F984.svg", "CC-BY-SA-4.0", "OpenMoji"),
        (
            "https://game-icons.net/icons/ffffff/000000/1x1/lorc/unicorn.svg",
            "CC-BY-3.0",
            "lorc",
        ),
        (
            "https://raw.githubusercontent.com/Templarian/MaterialDesign/master/svg/unicorn.svg",
            "Apache-2.0",
            "Pictogrammers",
        ),
        (
            "https://github.com/tabler/tabler-icons/raw/main/icons/outline/heart.svg",
            "MIT",
            "Paweł Kuna",
        ),
    ],
)
def test_the_catalogues_on_the_allowlist_have_a_known_licence(
    url: str, spdx: str, author: str
) -> None:
    licence = known_licence(url)
    assert licence is not None
    assert (licence.spdx, licence.author) == (spdx, author)
    assert licence.url and licence.url.startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.iconify.design/fa/unicorn.svg",
        "https://raw.githubusercontent.com/someone/else/main/a.svg",
        "https://example.com/a.svg",
        "https://notopenmoji.org/a.svg",
    ],
)
def test_anything_else_has_none(url: str) -> None:
    assert known_licence(url) is None


LICENCE = AssetLicence(
    spdx="CC-BY-SA-4.0",
    url="https://creativecommons.org/licenses/by-sa/4.0/",
    author="OpenMoji",
)
URL = "https://openmoji.org/data/color/svg/1F984.svg"


def test_a_credit_names_the_file_its_source_and_licence() -> None:
    assert credit_line("1F984.svg", URL, LICENCE) == (
        f"- `1F984.svg` from <{URL}>, by OpenMoji, under "
        "[CC-BY-SA-4.0](https://creativecommons.org/licenses/by-sa/4.0/)"
    )
    assert credit_line("a.svg", "https://example.com/a.svg", None) == (
        "- `a.svg` from <https://example.com/a.svg>, licence not stated"
    )


def test_a_credit_cannot_break_out_of_its_line() -> None:
    line = credit_line("a`\n# x.svg", URL, AssetLicence(name="Mine\n# Owned", author="[x](y)"))
    assert "\n" not in line
    assert "`a' # x.svg`" in line and "by \\[x\\]\\(y\\)" in line and "Mine \\# Owned" in line


def test_the_first_credit_starts_a_credits_section_and_the_next_join_it() -> None:
    first = with_credit("# Sign\n\nA sign.\n", URL, "- one")
    assert first == "# Sign\n\nA sign.\n\n## Credits\n\n- one\n"
    second = with_credit(first, "https://example.com/b.svg", "- two")
    assert second == "# Sign\n\nA sign.\n\n## Credits\n\n- one\n- two\n"
    assert with_credit("", URL, "- one") == "## Credits\n\n- one\n"


def test_a_source_already_credited_is_not_credited_again() -> None:
    line = credit_line("1F984.svg", URL, LICENCE)
    readme = with_credit("# Sign\n", URL, line)
    assert readme is not None
    assert with_credit(readme, URL, line) is None


def test_a_credits_section_followed_by_another_keeps_its_place() -> None:
    readme = "# Sign\n\n## Credits\n\n- one\n\n## Printing\n\nPLA.\n"
    assert with_credit(readme, "https://example.com/b.svg", "- two") == (
        "# Sign\n\n## Credits\n\n- one\n- two\n\n## Printing\n\nPLA.\n"
    )
