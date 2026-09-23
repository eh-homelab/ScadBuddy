from __future__ import annotations

import re

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"

_SLUG_RE = re.compile(SLUG_PATTERN)
_SEPARATORS = re.compile(r"[^a-z0-9]+")


class InvalidSlugError(ValueError):
    pass


def slugify(value: str) -> str:
    """Kebab-case a filename or title into a slug. Raises when nothing usable is left."""
    slug = _SEPARATORS.sub("-", value.strip().lower()).strip("-")
    if not _SLUG_RE.match(slug):
        raise InvalidSlugError(f"{value!r} does not yield a usable slug")
    return slug


def slug_from_filename(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.lower().endswith(".scad"):
        stem = stem[: -len(".scad")]
    return slugify(stem)
