from __future__ import annotations

import re

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"

_SLUG_RE = re.compile(SLUG_PATTERN)
_SEPARATORS = re.compile(r"[^a-z0-9]+")

#: A slug names directories and files (with suffixes such as a tombstone's), so it is
#: kept well inside the 255-byte limit on a file name: past it, creating the model
#: fails with an OSError instead of a clear refusal.
MAX_SLUG_LENGTH = 100


class InvalidSlugError(ValueError):
    pass


def slugify(value: str) -> str:
    """Kebab-case a filename or title into a slug. Raises when nothing usable is left."""
    slug = _SEPARATORS.sub("-", value.strip().lower()).strip("-")
    if not _SLUG_RE.match(slug):
        raise InvalidSlugError(f"{value!r} does not yield a usable slug")
    if len(slug) > MAX_SLUG_LENGTH:
        raise InvalidSlugError(f"{value!r} yields a slug longer than {MAX_SLUG_LENGTH} characters")
    return slug


def slug_from_filename(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.lower().endswith(".scad"):
        stem = stem[: -len(".scad")]
    return slugify(stem)
