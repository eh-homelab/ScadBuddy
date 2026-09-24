from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.core.fontconfig import conf_path
from scadbuddy.library.fonts import (
    MANIFEST_NAME,
    FontFamily,
    FontNotFoundError,
    FontService,
    InstalledFamily,
)
from scadbuddy.library.googlefonts import (
    CatalogueFont,
    FontCatalogue,
    FontVariant,
    GoogleFontsError,
)

PACIFICO = CatalogueFont(
    family="Pacifico",
    category="handwriting",
    variants=[FontVariant()],
    popularity=1,
    files={"regular": "https://fonts.gstatic.com/s/pacifico/v22/pacifico.ttf"},
)
ROBOTO = CatalogueFont(
    family="Roboto",
    category="sans-serif",
    variants=[FontVariant(), FontVariant(weight=700, italic=True)],
    popularity=2,
)

FONT_BYTES = b"\x00\x01\x00\x00not-really-a-ttf"


class FakeClient:
    """Stands in for GoogleFontsClient. Counts calls so the caching claims are testable."""

    def __init__(
        self,
        fonts: list[CatalogueFont] | None = None,
        *,
        fail: bool = False,
        licence: tuple[str, str] | None = ("OFL.txt", "Copyright (c) the authors"),
    ) -> None:
        self.fonts = fonts if fonts is not None else [PACIFICO, ROBOTO]
        self.fail = fail
        self.licence = licence
        self.fetches = 0
        self.downloads: list[str] = []

    async def fetch_catalogue(self) -> FontCatalogue:
        self.fetches += 1
        if self.fail:
            raise GoogleFontsError("no network")
        return FontCatalogue(
            source="google-fonts-metadata", fetched_at=datetime.now(UTC), fonts=self.fonts
        )

    async def resolve_files(self, font: CatalogueFont) -> dict[str, str]:
        if font.files:
            return dict(font.files)
        return {variant.api_key: f"https://x/{variant.api_key}.ttf" for variant in font.variants}

    async def fetch_file(self, url: str) -> bytes:
        self.downloads.append(url)
        return FONT_BYTES

    async def fetch_licence(self, family: str) -> tuple[str, str] | None:
        return self.licence


def service(tmp_path: Path, client: FakeClient | None = None, **kwargs: object) -> FontService:
    return FontService(tmp_path, client=client or FakeClient(), **kwargs)  # type: ignore[arg-type]


def test_prepare_writes_the_fontconfig_config(tmp_path: Path) -> None:
    service(tmp_path).prepare()
    assert conf_path(tmp_path).is_file()


async def test_the_catalogue_is_cached_on_the_data_volume(tmp_path: Path) -> None:
    client = FakeClient()
    fonts = service(tmp_path, client)

    first = await fonts.catalogue()
    second = await fonts.catalogue()

    assert client.fetches == 1
    assert [f.family for f in second.fonts] == [f.family for f in first.fonts]
    assert fonts.cache_file.is_file()


async def test_an_expired_cache_is_refetched(tmp_path: Path) -> None:
    client = FakeClient()
    fonts = service(tmp_path, client, catalogue_ttl=0.0)

    await fonts.catalogue()
    await fonts.catalogue()

    assert client.fetches == 2


async def test_a_stale_cache_beats_no_catalogue_when_the_fetch_fails(tmp_path: Path) -> None:
    fonts = service(tmp_path, FakeClient(), catalogue_ttl=0.0)
    await fonts.catalogue()
    stored = json.loads(fonts.cache_file.read_text(encoding="utf-8"))
    stored["fetched_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    fonts.cache_file.write_text(json.dumps(stored), encoding="utf-8")

    offline = service(tmp_path, FakeClient(fail=True), catalogue_ttl=0.0)
    assert [font.family for font in (await offline.catalogue()).fonts] == ["Pacifico", "Roboto"]


async def test_with_no_cache_at_all_a_failed_fetch_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GoogleFontsError):
        await service(tmp_path, FakeClient(fail=True)).catalogue()


async def test_an_unreadable_cache_is_refetched_rather_than_fatal(tmp_path: Path) -> None:
    fonts = service(tmp_path, FakeClient())
    fonts.root.mkdir(parents=True, exist_ok=True)
    fonts.cache_file.write_text("{ not json", encoding="utf-8")

    assert len((await fonts.catalogue()).fonts) == 2


async def test_installing_writes_the_files_the_licence_and_a_manifest(tmp_path: Path) -> None:
    fonts = service(tmp_path)

    installed = await fonts.install("Pacifico")

    directory = fonts.family_dir("Pacifico")
    assert (directory / "Pacifico-Regular.ttf").read_bytes() == FONT_BYTES
    assert (directory / "OFL.txt").read_text(encoding="utf-8").startswith("Copyright")
    manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["family"] == "Pacifico"
    assert manifest["licence"] == "OFL.txt"
    assert installed.files == ["Pacifico-Regular.ttf"]
    assert installed.licence == "OFL.txt"


async def test_every_variant_is_downloaded_and_named_by_its_style(tmp_path: Path) -> None:
    fonts = service(tmp_path)

    await fonts.install("Roboto")

    names = sorted(path.name for path in fonts.family_dir("Roboto").glob("*.ttf"))
    assert names == ["Roboto-BoldItalic.ttf", "Roboto-Regular.ttf"]


async def test_a_family_outside_the_catalogue_is_a_not_found(tmp_path: Path) -> None:
    with pytest.raises(FontNotFoundError):
        await service(tmp_path).install("Comic Sans MS")


async def test_the_lookup_is_case_insensitive(tmp_path: Path) -> None:
    installed = await service(tmp_path).install("pacifico")
    assert installed.family == "Pacifico"


async def test_a_licence_that_cannot_be_fetched_still_leaves_one_on_disk(tmp_path: Path) -> None:
    fonts = service(tmp_path, FakeClient(licence=None))

    installed = await fonts.install("Pacifico")

    text = (fonts.family_dir("Pacifico") / "LICENSE.txt").read_text(encoding="utf-8")
    assert "SIL Open Font" in text
    assert "fonts.google.com/specimen/Pacifico/license" in text
    assert installed.licence == "LICENSE.txt"


async def test_an_installed_family_is_returned_without_touching_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    fonts = service(tmp_path, client)
    monkeypatch.setattr(
        FontService, "installed", lambda self: [FontFamily(family="Pacifico", styles=["Regular"])]
    )

    installed = await fonts.install("Pacifico")

    assert installed == InstalledFamily(
        family="Pacifico", styles=["Regular"], files=[], licence=None
    )
    assert client.fetches == 0
    assert client.downloads == []


async def test_force_reinstalls_a_family_fontconfig_already_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    fonts = service(tmp_path, client)
    monkeypatch.setattr(FontService, "installed", lambda self: [])

    await fonts.install("Pacifico", force=True)

    assert client.downloads == ["https://fonts.gstatic.com/s/pacifico/v22/pacifico.ttf"]


REAL_FONTCONFIG = shutil.which("fc-list") and shutil.which("fc-cache")


@pytest.mark.skipif(not REAL_FONTCONFIG, reason="fontconfig is not installed")
async def test_a_font_on_the_data_volume_becomes_resolvable_after_a_real_fc_cache(
    tmp_path: Path,
) -> None:
    """The whole point of the fonts directory: OpenSCAD inherits this environment, so
    whatever `fc-list` reports here is what `text(font = ...)` can resolve."""
    listing = subprocess.run(
        ["fc-list", "--format", "%{file}\n"], capture_output=True, text=True, check=True
    ).stdout
    donor = next(
        (Path(line) for line in listing.splitlines() if line.strip().endswith((".ttf", ".otf"))),
        None,
    )
    if donor is None or not donor.is_file():
        pytest.skip("no system font file to copy")

    fonts = service(tmp_path)
    fonts.prepare()
    target = fonts.family_dir("Test Family")
    target.mkdir(parents=True, exist_ok=True)
    copied = target / donor.name
    shutil.copy(donor, copied)

    fonts.refresh_cache()

    seen = subprocess.run(
        ["fc-list", "--format", "%{file}\n"],
        capture_output=True,
        text=True,
        check=True,
        env=fonts.env(),
    ).stdout
    assert str(copied) in seen
