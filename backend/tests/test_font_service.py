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
    FontNotResolvedError,
    FontService,
    InstalledFamily,
    cut_by_dash,
    font_families,
    list_fonts,
    normalise_family,
    parse_fc_list,
    resolvable_families,
)
from scadbuddy.library.googlefonts import (
    CatalogueFont,
    FamilyFiles,
    FontCatalogue,
    FontFile,
    FontVariant,
    GoogleFontsError,
)

PACIFICO = CatalogueFont(
    family="Pacifico", category="handwriting", variants=[FontVariant()], popularity=1
)
ROBOTO = CatalogueFont(
    family="Roboto",
    category="sans-serif",
    variants=[FontVariant(), FontVariant(weight=700, italic=True)],
    popularity=2,
)

REPO_FILES = {
    "Pacifico": FamilyFiles(
        family="Pacifico",
        directory="ofl",
        licence="OFL",
        files=[
            FontFile(
                variant=FontVariant(),
                filename="Pacifico-Regular.ttf",
                url="https://raw/ofl/pacifico/Pacifico-Regular.ttf",
            )
        ],
    ),
    "Roboto": FamilyFiles(
        family="Roboto",
        directory="apache",
        licence="APACHE2",
        files=[
            FontFile(
                variant=FontVariant(),
                filename="Roboto-Regular.ttf",
                url="https://raw/apache/roboto/Roboto-Regular.ttf",
            ),
            FontFile(
                variant=FontVariant(weight=700, italic=True),
                filename="Roboto-BoldItalic.ttf",
                url="https://raw/apache/roboto/Roboto-BoldItalic.ttf",
            ),
        ],
    ),
}

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

    async def fetch_family_files(self, family: str) -> FamilyFiles:
        found = REPO_FILES.get(family)
        if found is None:
            raise GoogleFontsError(f"{family!r} is not in the google/fonts repository")
        return found

    async def fetch_file(self, url: str) -> bytes:
        self.downloads.append(url)
        return FONT_BYTES

    async def fetch_licence(self, files: FamilyFiles) -> tuple[str, str] | None:
        return self.licence


def service(tmp_path: Path, client: FakeClient | None = None, **kwargs: object) -> FontService:
    return FontService(tmp_path, client=client or FakeClient(), **kwargs)  # type: ignore[arg-type]


def _downloaded(self: FontService) -> set[str]:
    """What a working fc-cache would make resolvable: every family an install recorded."""
    return {
        normalise_family(json.loads(manifest.read_text(encoding="utf-8"))["family"])
        for manifest in self.root.glob(f"*/{MANIFEST_NAME}")
    }


@pytest.fixture(autouse=True)
def _downloads_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake TTFs here are not fonts, so no real fontconfig would resolve them;
    a test that is about resolution replaces this."""
    monkeypatch.setattr(FontService, "resolvable", _downloaded)


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


async def test_every_face_is_downloaded_under_the_name_google_ships_it_as(
    tmp_path: Path,
) -> None:
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

    assert client.downloads == ["https://raw/ofl/pacifico/Pacifico-Regular.ttf"]


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


def test_fc_tools_never_see_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """#281: list_fonts() without an env, or with a raw one, still runs fc-list on the allowlist."""
    seen: list[dict[str, str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(dict(kwargs["env"]))  # type: ignore[call-overload]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("SCADBUDDY_BAMBUDDY_API_KEY", "hunter2")

    list_fonts()
    list_fonts({"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "shh"})

    assert len(seen) == 2
    assert all("SCADBUDDY_BAMBUDDY_API_KEY" not in env for env in seen)
    assert seen[1] == {"PATH": "/usr/bin"}


# ── resolution (#253) ────────────────────────────────────────────────────────────


async def test_an_install_fontconfig_does_not_resolve_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files landing is not the family resolving: a render would fall back."""
    monkeypatch.setattr(FontService, "resolvable", lambda self: set())

    with pytest.raises(FontNotResolvedError) as raised:
        await service(tmp_path).install("Pacifico")

    assert raised.value.family == "Pacifico"
    assert raised.value.files == ["Pacifico-Regular.ttf"]


async def test_without_fontconfig_an_install_is_not_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FontService, "resolvable", lambda self: None)

    installed = await service(tmp_path).install("Pacifico")

    assert installed.files == ["Pacifico-Regular.ttf"]


def test_missing_families_compares_as_fontconfig_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FontService, "resolvable", lambda self: {normalise_family("DejaVu Sans")})

    assert service(tmp_path).missing_families(["dejavu sans", "DejaVuSans", "Lobster"]) == [
        "Lobster"
    ]


def test_a_font_string_names_its_families_as_fcnameparse_reads_them() -> None:
    assert font_families("Liberation Sans:style=Bold Italic") == ["Liberation Sans"]
    assert font_families("A, B:style=Bold") == ["A", "B"]
    assert font_families("Unifont-JP") == ["Unifont"]
    assert font_families("Unifont\\-JP:style=Regular") == ["Unifont-JP"]
    assert font_families("") == []
    assert font_families(":style=Bold") == []


def test_a_bare_dash_in_the_family_is_noticed() -> None:
    assert cut_by_dash("Unifont-JP:style=Regular")
    assert not cut_by_dash("Unifont\\-JP:style=Regular")
    assert not cut_by_dash("DejaVu Sans:style=Condensed-Bold")


def test_fc_list_escapes_are_undone() -> None:
    parsed = parse_fc_list("IBM 3270 Semi\\-Narrow,IBM 3270:style=Regular\n")
    assert parsed == [FontFamily(family="IBM 3270 Semi-Narrow", styles=["Regular"])]


def test_resolvable_families_keeps_every_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        out = "DejaVu Sans,DejaVu Sans Condensed\nIBM 3270 Semi\\-Narrow\n\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert resolvable_families({}) == {"dejavusans", "dejavusanscondensed", "ibm3270semi-narrow"}
    # Outline, scalable faces: what OpenSCAD's FontCache asks fontconfig for.
    assert seen == [["fc-list", ":outline=true:scalable=true", "family"]]


def test_without_fc_list_nothing_is_resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert resolvable_families({}) is None


@pytest.mark.skipif(not REAL_FONTCONFIG, reason="fontconfig is not installed")
def test_a_real_fontconfig_resolves_the_families_it_lists() -> None:
    families = list_fonts()
    known = resolvable_families()
    if not families or known is None:
        pytest.skip("fontconfig lists no fonts here")
    assert "nosuchfamilyanywhere" not in known
    assert any(normalise_family(family.family) in known for family in families)
