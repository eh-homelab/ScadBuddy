"""The Google Fonts catalogue, and pulling a family's files off it.

Two catalogue sources, picked by whether an API key is configured:

* ``SCADBUDDY_GOOGLE_FONTS_API_KEY`` set — the **Developer API**
  (``webfonts/v1/webfonts?sort=popularity``), the documented and stable one.
* no key — ``https://fonts.google.com/metadata/fonts``, the **public metadata** the
  fonts.google.com front end reads. No key, no quota, same families, same categories
  and a real popularity rank. It is not a documented API, and it may prefix its body
  with the XSSI guard ``)]}'`` (observed both with and without), so that is stripped
  when present.

The key never leaves the server: the browser only ever talks to ``/api/v1/fonts``.

**Downloads do not come from either.** They come from the ``google/fonts``
repository, whose ``METADATA.pb`` names the exact TTF for every face, and that holds
whether or not a key is set. The alternative — the CSS endpoint with an old
``User-Agent``, the trick google-webfonts-helper uses — was measured on 2026-09-23
and rejected on the evidence: an IE6/IE8 agent is served **EOT**, which fontconfig
cannot read at all, and the agents that do yield ``.ttf`` are served *per-subset*
files, so a family would install missing most of its glyphs. The repository serves
the complete static (or variable) font, names it deterministically, and carries the
family's licence next to it, which is the same fetch.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEVELOPER_API_URL = "https://www.googleapis.com/webfonts/v1/webfonts"
METADATA_URL = "https://fonts.google.com/metadata/fonts"
REPO_BASE_URL = "https://raw.githubusercontent.com/google/fonts/main"
METADATA_PB = "METADATA.pb"
XSSI_PREFIX = ")]}'"
DEFAULT_TIMEOUT = 30.0

CatalogueSource = Literal["developer-api", "google-fonts-metadata"]

# Google Fonts is OFL, Apache-2.0 or UFL, and the repository lays the families out
# under one directory per licence. Which one a family is in is not derivable from the
# catalogue, so it is discovered by probing — and the answer also names the licence
# file to keep beside the font.
LICENCE_DIRECTORIES: tuple[str, ...] = ("ofl", "apache", "ufl")
LICENCE_FILENAMES: dict[str, str] = {"OFL": "OFL.txt", "APACHE2": "LICENSE.txt", "UFL": "UFL.txt"}

_WEIGHT_NAMES = {
    100: "Thin",
    200: "ExtraLight",
    300: "Light",
    400: "Regular",
    500: "Medium",
    600: "SemiBold",
    700: "Bold",
    800: "ExtraBold",
    900: "Black",
}

_VARIANT_RE = re.compile(r"^(?P<weight>\d{3})?(?P<italic>italic|i)?$")
# METADATA.pb is protobuf *text* format. The `fonts { ... }` blocks hold no nested
# braces, so one shallow match per block is enough and no protobuf runtime is needed.
_PB_FONTS_RE = re.compile(r"\bfonts\s*\{(?P<body>[^{}]*)\}")
_PB_LICENSE_RE = re.compile(r'^license:\s*"([^"]*)"', re.MULTILINE)


class GoogleFontsError(RuntimeError):
    """The catalogue or a font file could not be fetched."""


class FontVariant(BaseModel):
    weight: int = 400
    italic: bool = False

    @property
    def style(self) -> str:
        """The fontconfig style name, which is what ``Family:style=…`` is written with."""
        return style_name(self.weight, self.italic)


class CatalogueFont(BaseModel):
    family: str
    category: str = ""
    variants: list[FontVariant] = Field(default_factory=list)
    # Rank, 1 = most popular. Both sources order by it; only one states it.
    popularity: int | None = None


class FontFile(BaseModel):
    variant: FontVariant
    filename: str
    """Google's own filename, kept verbatim: it is what the family ships as."""
    url: str


class FamilyFiles(BaseModel):
    """One family as the ``google/fonts`` repository holds it."""

    family: str
    directory: str
    licence: str = ""
    files: list[FontFile] = Field(default_factory=list)


class FontCatalogue(BaseModel):
    source: CatalogueSource
    fetched_at: datetime
    fonts: list[CatalogueFont] = Field(default_factory=list)

    def find(self, family: str) -> CatalogueFont | None:
        folded = family.casefold()
        return next((font for font in self.fonts if font.family.casefold() == folded), None)


def style_name(weight: int, italic: bool) -> str:
    """``(700, False) -> "Bold"``, ``(400, True) -> "Italic"``,
    ``(300, True) -> "Light Italic"``."""
    base = _WEIGHT_NAMES.get(weight, str(weight))
    if not italic:
        return base
    return "Italic" if weight == 400 else f"{base} Italic"


def parse_variant(raw: str) -> FontVariant:
    """Both sources spell a variant differently: ``regular``/``700italic`` and ``400``/``700i``."""
    token = raw.strip().lower()
    if token in ("regular", ""):
        return FontVariant()
    match = _VARIANT_RE.match(token)
    if match is None:
        raise ValueError(f"unrecognised font variant {raw!r}")
    weight = int(match["weight"]) if match["weight"] else 400
    return FontVariant(weight=weight, italic=bool(match["italic"]))


def _sorted_variants(variants: Iterable[FontVariant]) -> list[FontVariant]:
    unique = {(variant.italic, variant.weight) for variant in variants}
    return [FontVariant(weight=weight, italic=italic) for italic, weight in sorted(unique)]


def parse_developer_api(payload: dict[str, Any]) -> list[CatalogueFont]:
    """``?sort=popularity`` orders the rows, so the rank is the position."""
    fonts: list[CatalogueFont] = []
    for rank, item in enumerate(payload.get("items", []), start=1):
        family = str(item.get("family", "")).strip()
        if not family:
            continue
        variants: list[FontVariant] = []
        for raw in item.get("variants", []):
            try:
                variants.append(parse_variant(str(raw)))
            except ValueError:
                logger.debug("skipping variant", extra={"family": family, "variant": raw})
        fonts.append(
            CatalogueFont(
                family=family,
                category=str(item.get("category", "")),
                variants=_sorted_variants(variants) or [FontVariant()],
                popularity=rank,
            )
        )
    return fonts


def parse_metadata(payload: dict[str, Any]) -> list[CatalogueFont]:
    """``familyMetadataList`` rows. ``category`` is title-cased there ("Sans Serif")."""
    fonts: list[CatalogueFont] = []
    for item in payload.get("familyMetadataList", []):
        family = str(item.get("family", "")).strip()
        if not family:
            continue
        variants: list[FontVariant] = []
        for raw in item.get("fonts") or {}:
            try:
                variants.append(parse_variant(str(raw)))
            except ValueError:
                logger.debug("skipping variant", extra={"family": family, "variant": raw})
        popularity = item.get("popularity")
        fonts.append(
            CatalogueFont(
                family=family,
                category=str(item.get("category", "")).strip().lower().replace(" ", "-"),
                variants=_sorted_variants(variants) or [FontVariant()],
                popularity=int(popularity) if isinstance(popularity, int) else None,
            )
        )
    fonts.sort(key=lambda font: (font.popularity is None, font.popularity or 0, font.family))
    return fonts


def strip_xssi(body: str) -> str:
    """``fonts.google.com/metadata/fonts`` guards its JSON with a ``)]}'`` line."""
    text = body.lstrip()
    return text[len(XSSI_PREFIX) :].lstrip() if text.startswith(XSSI_PREFIX) else text


def parse_family_metadata(
    text: str, *, family: str, directory: str, base_url: str = REPO_BASE_URL
) -> FamilyFiles:
    """A ``METADATA.pb`` into the files to download.

    Variable fonts name themselves ``NotoSans[wdth,wght].ttf`` and are listed once per
    named instance, so the same filename legitimately appears against several weights;
    it is downloaded once and fontconfig reports every instance as a style.
    """
    slug = licence_slug(family)
    licence_match = _PB_LICENSE_RE.search(text)
    files: list[FontFile] = []
    seen: set[str] = set()
    for block in _PB_FONTS_RE.finditer(text):
        body = block["body"]
        filename = _pb_string(body, "filename")
        if not filename or filename in seen:
            continue
        seen.add(filename)
        weight = _pb_number(body, "weight")
        files.append(
            FontFile(
                variant=FontVariant(
                    weight=weight if weight is not None else 400,
                    italic=_pb_string(body, "style") == "italic",
                ),
                filename=filename,
                # `,` is left alone and `[`/`]` are escaped, which is what raw
                # .githubusercontent.com was verified to accept for a variable font.
                url=f"{base_url}/{directory}/{slug}/{quote(filename, safe=',')}",
            )
        )
    return FamilyFiles(
        family=family,
        directory=directory,
        licence=licence_match[1] if licence_match else "",
        files=files,
    )


def _pb_string(body: str, field: str) -> str:
    match = re.search(rf'\b{field}:\s*"([^"]*)"', body)
    return match[1] if match else ""


def _pb_number(body: str, field: str) -> int | None:
    match = re.search(rf"\b{field}:\s*(\d+)", body)
    return int(match[1]) if match else None


def licence_slug(family: str) -> str:
    """The family's directory in the ``google/fonts`` repo: lower-cased, no spaces."""
    return re.sub(r"[^a-z0-9]", "", family.casefold())


class GoogleFontsClient:
    """Reads the catalogue and resolves a family's downloadable files."""

    def __init__(self, api_key: str | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.api_key = api_key or None
        self.timeout = timeout

    @property
    def source(self) -> CatalogueSource:
        return "developer-api" if self.api_key else "google-fonts-metadata"

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise GoogleFontsError(f"{url} is unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise GoogleFontsError(f"{url} answered {response.status_code}")
        return response

    async def fetch_catalogue(self) -> FontCatalogue:
        if self.api_key:
            response = await self._get(
                DEVELOPER_API_URL, params={"key": self.api_key, "sort": "popularity"}
            )
            fonts = parse_developer_api(_json_body(response.text, DEVELOPER_API_URL))
        else:
            response = await self._get(METADATA_URL)
            fonts = parse_metadata(_json_body(strip_xssi(response.text), METADATA_URL))
        if not fonts:
            raise GoogleFontsError("the font catalogue came back empty")
        return FontCatalogue(source=self.source, fetched_at=datetime.now(UTC), fonts=fonts)

    async def fetch_family_files(self, family: str) -> FamilyFiles:
        """Find the family in the ``google/fonts`` repository and list its TTFs.

        Which licence directory holds it is not in either catalogue, so the three are
        probed in turn; the one that answers also tells us the licence.
        """
        slug = licence_slug(family)
        if not slug:
            raise GoogleFontsError(f"{family!r} has no usable directory name")
        for directory in LICENCE_DIRECTORIES:
            try:
                response = await self._get(f"{REPO_BASE_URL}/{directory}/{slug}/{METADATA_PB}")
            except GoogleFontsError:
                continue
            files = parse_family_metadata(response.text, family=family, directory=directory)
            if not files.files:
                raise GoogleFontsError(f"{family!r} lists no font files")
            return files
        raise GoogleFontsError(f"{family!r} is not in the google/fonts repository")

    async def fetch_file(self, url: str) -> bytes:
        return (await self._get(url)).content

    async def fetch_licence(self, files: FamilyFiles) -> tuple[str, str] | None:
        """The family's licence text, or None when the repository does not carry one."""
        slug = licence_slug(files.family)
        named = LICENCE_FILENAMES.get(files.licence)
        candidates = (
            [named, *LICENCE_FILENAMES.values()] if named else [*LICENCE_FILENAMES.values()]
        )
        for filename in dict.fromkeys(filter(None, candidates)):
            try:
                response = await self._get(f"{REPO_BASE_URL}/{files.directory}/{slug}/{filename}")
            except GoogleFontsError:
                continue
            return filename, response.text
        logger.info("no licence file found for %s", files.family)
        return None


def _json_body(text: str, url: str) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GoogleFontsError(f"{url} did not answer with JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GoogleFontsError(f"{url} answered with {type(parsed).__name__}, not an object")
    return parsed
