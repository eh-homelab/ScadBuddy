"""The Google Fonts catalogue, and pulling a family's files off it.

Two catalogue sources, picked by whether an API key is configured:

* ``SCADBUDDY_GOOGLE_FONTS_API_KEY`` set — the **Developer API**
  (``webfonts/v1/webfonts``). It is the documented, stable one, and its rows carry a
  ``files`` map of direct ``fonts.gstatic.com`` TTF URLs, so installing a family needs
  no second lookup.
* no key — ``https://fonts.google.com/metadata/fonts``, the **public metadata** the
  fonts.google.com front end reads. No key, no quota, same families, same categories
  and a real popularity rank. It is not a documented API and its body is prefixed
  with the XSSI guard ``)]}'``, which has to be stripped before the JSON parses.

The key never leaves the server: the browser only ever talks to ``/api/v1/fonts``.

The keyless path has no file URLs, so downloads go through the **CSS API** with a
legacy ``User-Agent``. Google serves WOFF2 to anything modern and plain TrueType to
a browser too old to know about it — and TrueType is the only one fontconfig, and so
OpenSCAD, can use. This is the same trick google-webfonts-helper uses; it is a
behaviour of the CSS endpoint rather than a documented contract, so
``parse_css_faces`` raising is a normal, reported outcome rather than an assertion.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEVELOPER_API_URL = "https://www.googleapis.com/webfonts/v1/webfonts"
METADATA_URL = "https://fonts.google.com/metadata/fonts"
CSS_URL = "https://fonts.googleapis.com/css2"
LICENCE_BASE_URL = "https://raw.githubusercontent.com/google/fonts/main"

# Old enough that the CSS endpoint answers with TrueType instead of WOFF2.
LEGACY_USER_AGENT = "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)"
XSSI_PREFIX = ")]}'"
DEFAULT_TIMEOUT = 30.0

CatalogueSource = Literal["developer-api", "google-fonts-metadata"]

# Google Fonts is OFL, Apache-2.0 or UFL; the repository lays the families out under
# one directory per licence, which is also how the licence text is found.
LICENCE_FILES: tuple[tuple[str, str], ...] = (
    ("ofl", "OFL.txt"),
    ("apache", "LICENSE.txt"),
    ("ufl", "UFL.txt"),
)

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
_FACE_RE = re.compile(r"@font-face\s*\{(?P<body>[^}]*)\}")
_WEIGHT_DECL_RE = re.compile(r"font-weight:\s*(\d{3})")
_STYLE_DECL_RE = re.compile(r"font-style:\s*(normal|italic)")
_SRC_URL_RE = re.compile(r"url\((?P<url>[^)]+)\)")


class GoogleFontsError(RuntimeError):
    """The catalogue or a font file could not be fetched."""


class FontVariant(BaseModel):
    weight: int = 400
    italic: bool = False

    @property
    def style(self) -> str:
        """The fontconfig style name, which is what ``Family:style=…`` is written with."""
        return style_name(self.weight, self.italic)

    @property
    def api_key(self) -> str:
        """How the Developer API keys this variant in its ``files`` map."""
        if self.weight == 400:
            return "italic" if self.italic else "regular"
        return f"{self.weight}italic" if self.italic else str(self.weight)


class CatalogueFont(BaseModel):
    family: str
    category: str = ""
    variants: list[FontVariant] = Field(default_factory=list)
    # Rank, 1 = most popular. Both sources order by it; only one states it.
    popularity: int | None = None
    # Direct TTF URLs keyed by ``FontVariant.api_key``. Developer API only.
    files: dict[str, str] = Field(default_factory=dict)


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
        files = {str(k): str(v) for k, v in (item.get("files") or {}).items()}
        fonts.append(
            CatalogueFont(
                family=family,
                category=str(item.get("category", "")),
                variants=_sorted_variants(variants) or [FontVariant()],
                popularity=rank,
                files=files,
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


def css2_family_query(family: str, variants: Sequence[FontVariant]) -> str:
    """``Pacifico:wght@400`` / ``Roboto:ital,wght@0,400;1,700`` — axes ascending, as the
    CSS2 endpoint requires; anything else is a 400."""
    pairs = sorted({(1 if v.italic else 0, v.weight) for v in variants}) or [(0, 400)]
    if any(italic for italic, _ in pairs):
        spec = "ital,wght@" + ";".join(f"{italic},{weight}" for italic, weight in pairs)
    else:
        spec = "wght@" + ";".join(str(weight) for _, weight in pairs)
    return f"{family}:{spec}"


def parse_css_faces(css: str) -> dict[str, str]:
    """``@font-face`` blocks to ``{variant key: url}``, keeping only TrueType/OpenType."""
    files: dict[str, str] = {}
    for face in _FACE_RE.finditer(css):
        body = face["body"]
        weight_match = _WEIGHT_DECL_RE.search(body)
        style_match = _STYLE_DECL_RE.search(body)
        url_match = _SRC_URL_RE.search(body)
        if url_match is None:
            continue
        url = url_match["url"].strip("'\" ")
        if not url.lower().endswith((".ttf", ".otf")):
            continue
        variant = FontVariant(
            weight=int(weight_match[1]) if weight_match else 400,
            italic=bool(style_match and style_match[1] == "italic"),
        )
        files.setdefault(variant.api_key, url)
    return files


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

    async def resolve_files(self, font: CatalogueFont) -> dict[str, str]:
        """``{variant key: TTF url}``. Free with a key; a CSS lookup without one."""
        if font.files:
            return {key: url for key, url in font.files.items() if url.lower().endswith(".ttf")}
        response = await self._get(
            CSS_URL,
            params={"family": css2_family_query(font.family, font.variants)},
            headers={"User-Agent": LEGACY_USER_AGENT},
        )
        files = parse_css_faces(response.text)
        if not files:
            raise GoogleFontsError(
                f"the CSS endpoint returned no TrueType files for {font.family!r}"
            )
        return files

    async def fetch_file(self, url: str) -> bytes:
        return (await self._get(url)).content

    async def fetch_licence(self, family: str) -> tuple[str, str] | None:
        """The family's licence text, or None when the repository layout does not match."""
        slug = licence_slug(family)
        for directory, filename in LICENCE_FILES:
            try:
                response = await self._get(f"{LICENCE_BASE_URL}/{directory}/{slug}/{filename}")
            except GoogleFontsError:
                continue
            return filename, response.text
        logger.info("no licence file found for %s", family)
        return None


def _json_body(text: str, url: str) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GoogleFontsError(f"{url} did not answer with JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GoogleFontsError(f"{url} answered with {type(parsed).__name__}, not an object")
    return parsed
