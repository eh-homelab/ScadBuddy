"""What fonts the renderer can see, and putting new ones where it will see them.

``list_installed`` is the ground truth — it asks fontconfig, so it covers both the
families baked into the image and anything downloaded onto the data volume. A
catalogue row is reported as installed only when fontconfig agrees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from scadbuddy.core.fontconfig import env_for, fonts_dir, write_conf
from scadbuddy.library.googlefonts import (
    CatalogueFont,
    FamilyFiles,
    FontCatalogue,
    GoogleFontsClient,
    GoogleFontsError,
    licence_slug,
)

FC_LIST = "fc-list"
FC_CACHE = "fc-cache"
FC_TIMEOUT = 30.0

CATALOGUE_CACHE_NAME = ".catalogue.json"
MANIFEST_NAME = "family.json"
FALLBACK_LICENCE_NAME = "LICENSE.txt"
DEFAULT_CATALOGUE_TTL = 86400.0

FALLBACK_LICENCE = """\
{family} was downloaded from Google Fonts by ScadBuddy.

Every family in the Google Fonts catalogue is released under the SIL Open Font
License 1.1, the Apache License 2.0 or the Ubuntu Font Licence 1.0. The licence
text for this family could not be fetched automatically; it is published at
https://fonts.google.com/specimen/{specimen}/license
"""

logger = logging.getLogger(__name__)


class FontFamily(BaseModel):
    family: str
    styles: list[str]


class InstalledFamily(BaseModel):
    """What an install left on disk, reported back with fontconfig's own style names."""

    family: str
    styles: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    licence: str | None = None


class FontNotFoundError(KeyError):
    """The requested family is not in the catalogue."""


def parse_fc_list(output: str) -> list[FontFamily]:
    """``fc-list : family style`` prints ``Family[,alias]:style=Style[,alias]``.

    Only the first family and first style of each line are kept: those are the names
    OpenSCAD's ``"Family:style=Style"`` font string is written with.
    """
    families: dict[str, set[str]] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        head, separator, tail = line.partition(":style=")
        family = head.split(",", 1)[0].strip()
        if not family:
            continue
        style = tail.split(",", 1)[0].strip() if separator else ""
        styles = families.setdefault(family, set())
        if style:
            styles.add(style)
    return [
        FontFamily(family=family, styles=sorted(families[family])) for family in sorted(families)
    ]


def _run_fc(argv: list[str], env: Mapping[str, str]) -> str | None:
    if shutil.which(argv[0]) is None:
        logger.warning("%s is not on PATH", argv[0])
        return None
    try:
        completed = subprocess.run(  # fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=FC_TIMEOUT,
            check=True,
            env=dict(env),
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("%s failed", argv[0])
        return None
    return completed.stdout


def list_fonts(env: Mapping[str, str] | None = None) -> list[FontFamily]:
    """Font families fontconfig can resolve, or an empty list without fontconfig."""
    output = _run_fc([FC_LIST, ":", "family", "style"], os.environ if env is None else env)
    return parse_fc_list(output) if output is not None else []


class FontService:
    """``<data>/fonts/`` — the catalogue cache, the downloaded families, the fontconfig
    config that makes OpenSCAD see them."""

    def __init__(
        self,
        data_dir: Path,
        *,
        api_key: str | None = None,
        catalogue_ttl: float = DEFAULT_CATALOGUE_TTL,
        client: GoogleFontsClient | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.catalogue_ttl = catalogue_ttl
        self.client = client or GoogleFontsClient(api_key)

    @property
    def root(self) -> Path:
        return fonts_dir(self.data_dir)

    @property
    def cache_file(self) -> Path:
        return self.root / CATALOGUE_CACHE_NAME

    def family_dir(self, family: str) -> Path:
        return self.root / licence_slug(family)

    def env(self) -> dict[str, str]:
        return env_for(self.data_dir)

    def prepare(self) -> None:
        """Write the fontconfig config. Idempotent; called once per startup."""
        write_conf(self.data_dir)

    def installed(self) -> list[FontFamily]:
        return list_fonts(self.env())

    def installed_families(self) -> set[str]:
        return {family.family.casefold() for family in self.installed()}

    def refresh_cache(self) -> None:
        if _run_fc([FC_CACHE, "--force", str(self.root)], self.env()) is None:
            logger.warning("fc-cache did not run; the new fonts may not resolve until restart")

    # ── catalogue ────────────────────────────────────────────────────────────────

    def load_cached_catalogue(self) -> FontCatalogue | None:
        if not self.cache_file.is_file():
            return None
        try:
            cached = FontCatalogue.model_validate_json(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            logger.warning("the cached font catalogue is unreadable; refetching")
            return None
        age = (datetime.now(UTC) - cached.fetched_at).total_seconds()
        return cached if age < self.catalogue_ttl else None

    def store_catalogue(self, catalogue: FontCatalogue) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(catalogue.model_dump_json(indent=2), encoding="utf-8")

    async def catalogue(self, *, refresh: bool = False) -> FontCatalogue:
        """The cached catalogue while it is fresh, otherwise a fetch.

        A fetch failure falls back to a stale cache when there is one: an expired
        catalogue is a far better answer than none, and the air-gapped case would
        otherwise lose the browse list entirely.
        """
        if not refresh:
            cached = self.load_cached_catalogue()
            if cached is not None:
                return cached
        try:
            fetched = await self.client.fetch_catalogue()
        except GoogleFontsError:
            stale = self._read_cache_ignoring_age()
            if stale is None:
                raise
            logger.warning("serving a stale font catalogue: the fetch failed")
            return stale
        self.store_catalogue(fetched)
        return fetched

    def _read_cache_ignoring_age(self) -> FontCatalogue | None:
        if not self.cache_file.is_file():
            return None
        try:
            return FontCatalogue.model_validate_json(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    # ── install ──────────────────────────────────────────────────────────────────

    def installed_family(self, family: str) -> InstalledFamily | None:
        """What fontconfig already has for ``family``, whether baked into the image or
        downloaded earlier. None when it has never heard of it."""
        folded = family.casefold()
        match = next((f for f in self.installed() if f.family.casefold() == folded), None)
        if match is None:
            return None
        manifest = self.family_dir(match.family) / MANIFEST_NAME
        recorded: dict[str, object] = {}
        if manifest.is_file():
            try:
                loaded = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                recorded = loaded
        files = recorded.get("files")
        licence = recorded.get("licence")
        return InstalledFamily(
            family=match.family,
            styles=match.styles,
            files=[str(name) for name in files] if isinstance(files, list) else [],
            licence=str(licence) if isinstance(licence, str) else None,
        )

    async def install(self, family: str, *, force: bool = False) -> InstalledFamily:
        """Download every face of ``family`` onto the data volume and refresh the cache.

        A family fontconfig already resolves is returned as-is and nothing is fetched,
        so picking one of the image's own faces works with no network at all.
        """
        if not force:
            existing = await asyncio.to_thread(self.installed_family, family)
            if existing is not None:
                return existing
        catalogue = await self.catalogue()
        font = catalogue.find(family)
        if font is None:
            raise FontNotFoundError(family)
        sources = await self.client.fetch_family_files(font.family)
        written = await self._download(font.family, sources)
        licence = await self._write_licence(font.family, sources)
        self._write_manifest(font, written, licence)
        # fc-cache walks the whole font tree and is slow enough to stall the loop.
        await asyncio.to_thread(self.refresh_cache)
        return InstalledFamily(
            family=font.family,
            styles=await asyncio.to_thread(self._styles_of, font),
            files=written,
            licence=licence,
        )

    async def _download(self, family: str, sources: FamilyFiles) -> list[str]:
        directory = self.family_dir(family)
        directory.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for source in sources.files:
            (directory / source.filename).write_bytes(await self.client.fetch_file(source.url))
            written.append(source.filename)
        if not written:
            raise GoogleFontsError(f"no font files were downloaded for {family!r}")
        return written

    async def _write_licence(self, family: str, sources: FamilyFiles) -> str:
        directory = self.family_dir(family)
        try:
            fetched = await self.client.fetch_licence(sources)
        except GoogleFontsError:
            fetched = None
        if fetched is not None:
            filename, text = fetched
            (directory / filename).write_text(text, encoding="utf-8")
            return filename
        (directory / FALLBACK_LICENCE_NAME).write_text(
            FALLBACK_LICENCE.format(family=family, specimen=family.replace(" ", "+")),
            encoding="utf-8",
        )
        return FALLBACK_LICENCE_NAME

    def _write_manifest(self, font: CatalogueFont, files: list[str], licence: str) -> None:
        (self.family_dir(font.family) / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "family": font.family,
                    "category": font.category,
                    "files": files,
                    "licence": licence,
                    "source": "https://fonts.google.com/specimen/" + font.family.replace(" ", "+"),
                    "installed_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _styles_of(self, font: CatalogueFont) -> list[str]:
        """fontconfig's own style names, which are what ``Family:style=…`` must carry.

        They are read back rather than derived: a family's TTFs name their own
        subfamily, and Google's do not always match the weight table (Debian's
        ``fonts-lobster`` is the standing example — its file is family "Lobster Two").
        """
        folded = font.family.casefold()
        for installed in self.installed():
            if installed.family.casefold() == folded:
                return installed.styles
        return [variant.style for variant in font.variants]
