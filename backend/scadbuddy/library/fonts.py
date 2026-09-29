"""What fonts the renderer can see, and putting new ones where it will see them.

``list_installed`` is the ground truth — it asks fontconfig, so it covers both the
families baked into the image and anything downloaded onto the data volume. A
catalogue row is reported as installed only when fontconfig agrees.

That is also what OpenSCAD sees. Its ``FontCache`` (src/FontCache.cc,
https://github.com/openscad/openscad/blob/master/src/FontCache.cc) loads fontconfig's
config and adds only its bundled ``fonts`` resource directory and ``$HOME/.fonts``;
``openscad --info`` in a local build of the image (measured 2026-09-29, #253) lists
fontconfig's own directories and ``$HOME/.fonts``, and no bundled one. So ``fc-list``
under the render's environment (:func:`~scadbuddy.core.fontconfig.env_for`) answers
for the render.

What OpenSCAD cannot do is refuse a missing family: ``find_face_fontconfig`` parses
the font string with ``FcNameParse`` and takes ``FcFontMatch``'s best match, which for
a family that is not installed is the default font (DejaVu Sans in this image),
silently, with other geometry. :meth:`FontService.missing_families` is how a caller
refuses one first (#253).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from scadbuddy.core.fontconfig import env_for, fonts_dir, minimal_env, write_conf
from scadbuddy.library.googlefonts import (
    CatalogueFont,
    FamilyFiles,
    FontCatalogue,
    GoogleFontsClient,
    GoogleFontsError,
    licence_slug,
)

FC_LIST = "fc-list"
#: The faces OpenSCAD's ``FontCache::init_pattern`` asks fontconfig for.
RENDERABLE = ":outline=true:scalable=true"
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


class FontNotResolvedError(RuntimeError):
    """An install wrote a family's files, but fontconfig does not resolve the family
    afterwards, so a render naming it would fall back to the default font (#253)."""

    def __init__(self, family: str, files: list[str]) -> None:
        super().__init__(family)
        self.family = family
        self.files = files


def _split_fc(text: str, separators: str, stop: str = "") -> list[str]:
    """``text`` split at each unescaped character of ``separators``, and cut at the
    first unescaped one of ``stop``, with fontconfig's backslash escapes undone.

    fontconfig's name syntax escapes ``\\``, ``-``, ``:`` and ``,`` with a backslash:
    ``FcNameUnparse`` writes them so (``fc-list`` prints "IBM 3270 Semi\\-Narrow") and
    ``FcNameParse`` reads them so ("Font Names",
    https://www.freedesktop.org/software/fontconfig/fontconfig-user.html).
    """
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in stop:
            break
        elif char in separators:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def normalise_family(family: str) -> str:
    """A family name as fontconfig compares it: ignoring case and blanks
    (``FcStrCmpIgnoreBlanksAndCase``,
    https://www.freedesktop.org/software/fontconfig/fontconfig-devel/fcstrcmpignoreblanksandcase.html)."""
    return family.replace(" ", "").casefold()


def font_families(font: str) -> list[str]:
    """The families an OpenSCAD font string names, read the way ``FcNameParse`` reads
    it: up to the first unescaped ``-`` (a point size follows) or ``:`` (properties),
    split at unescaped commas (a fallback list).

    ``"Liberation Sans:style=Bold"`` names one family; ``""`` and ``":style=Bold"``
    name none, which is the default font.
    """
    return [family.strip() for family in _split_fc(font, ",", stop="-:") if family.strip()]


def cut_by_dash(font: str) -> bool:
    """Whether a bare ``-`` ends the family part of ``font`` before its ``:``, which
    fontconfig reads as the start of a point size: ``"Unifont-JP"`` is family
    "Unifont" at size "JP". Written ``"Unifont\\-JP"`` it is the family."""
    return _split_fc(font, "", stop=":")[0] != _split_fc(font, "", stop="-:")[0]


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
        # Unescaped: a family is listed as OpenSCAD matches it, not as fc-list quotes it.
        family = _split_fc(head, ",")[0].strip()
        if not family:
            continue
        style = _split_fc(tail, ",")[0].strip() if separator else ""
        styles = families.setdefault(family, set())
        if style:
            styles.add(style)
    return [
        FontFamily(family=family, styles=sorted(families[family])) for family in sorted(families)
    ]


def _run_fc(argv: list[str], env: Mapping[str, str]) -> str | None:
    """Run an fc-* tool on the allowlisted environment (#281), whatever ``env`` holds."""
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
            env=minimal_env(env),
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("%s failed", argv[0])
        return None
    return completed.stdout


def list_fonts(
    env: Mapping[str, str] | None = None, *, renderable: bool = False
) -> list[FontFamily]:
    """Font families fontconfig can resolve, or an empty list without fontconfig.

    Every face by default, as ``GET /fonts`` lists them; ``renderable`` keeps the
    outline, scalable ones a render can use, as :func:`resolvable_families` does.
    """
    pattern = RENDERABLE if renderable else ":"
    output = _run_fc([FC_LIST, pattern, "family", "style"], os.environ if env is None else env)
    return parse_fc_list(output) if output is not None else []


def resolvable_families(env: Mapping[str, str] | None = None) -> set[str] | None:
    """Every family name fontconfig resolves, each alias included, as
    :func:`normalise_family` writes it; None without fontconfig to ask.

    Outline, scalable faces only: the ones OpenSCAD's ``FontCache::init_pattern`` asks
    fontconfig for.
    """
    output = _run_fc([FC_LIST, RENDERABLE, "family"], os.environ if env is None else env)
    if output is None:
        return None
    return {
        normalise_family(name)
        for line in output.splitlines()
        for name in _split_fc(line.strip(), ",")
        if name.strip()
    }


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
        #: Set when the key changes (#322): the cached catalogue came from the other
        #: source, so the next read fetches. A failed fetch still serves the cache.
        self.catalogue_stale = False

    def use_api_key(self, api_key: str | None) -> None:
        """Fetch the catalogue with ``api_key`` from now on, and refetch it."""
        if (api_key or None) == self.client.api_key:
            return
        self.client = GoogleFontsClient(api_key, timeout=self.client.timeout)
        self.catalogue_stale = True

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
        """Every family fontconfig lists, a bitmap one included: ``GET /fonts``."""
        return list_fonts(self.env())

    def renderable(self) -> list[FontFamily]:
        """Those of :meth:`installed` a render can draw, by :meth:`resolvable`'s filter.
        What "already installed" means everywhere a family is judged, so an install
        that finds one never disagrees with the render that uses it (review of #740)."""
        return list_fonts(self.env(), renderable=True)

    def installed_families(self) -> set[str]:
        return {family.family.casefold() for family in self.renderable()}

    def resolvable(self) -> set[str] | None:
        """:func:`resolvable_families` under the render's own environment."""
        return resolvable_families(self.env())

    def missing_families(self, families: Iterable[str]) -> list[str] | None:
        """Those of ``families`` fontconfig does not resolve, which a render would
        silently draw in the default font instead; None without fontconfig to ask."""
        known = self.resolvable()
        if known is None:
            return None
        return [family for family in families if normalise_family(family) not in known]

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
        if not refresh and not self.catalogue_stale:
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
        self.catalogue_stale = False
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
        downloaded earlier. None when it has never heard of it, or has it only in a face
        a render cannot use (a bitmap one)."""
        wanted = normalise_family(family)
        match = next((f for f in self.renderable() if normalise_family(f.family) == wanted), None)
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
        # Files on disk are not a family that resolves: a cache fc-cache did not
        # rebuild, a face fontconfig will not load, or TTFs that name another family
        # all leave a render falling back to the default font (#253). Asked the way
        # a render asks, under its environment. Without fontconfig nothing can be
        # checked, and no render could use the family either.
        missing = await asyncio.to_thread(self.missing_families, [font.family])
        if missing:
            raise FontNotResolvedError(font.family, written)
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
        for installed in self.renderable():
            if installed.family.casefold() == folded:
                return installed.styles
        return [variant.style for variant in font.variants]
