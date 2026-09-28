from __future__ import annotations

import re

from scadbuddy.core.paths import BUILTIN_PREFIX

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"

#: A slug names directories and files (with suffixes such as a tombstone's), so it is
#: kept well inside the 255-byte limit on a file name: past it, creating the model
#: fails with an OSError instead of a clear refusal.
MAX_SLUG_LENGTH = 100
#: A template of mine (a bare slug) or a built-in (`builtin:<slug>`). Either way the
#: slug is at most MAX_SLUG_LENGTH, which the pattern itself states (#202).
MODEL_ID_PATTERN = rf"^({re.escape(BUILTIN_PREFIX)})?[a-z0-9][a-z0-9-]{{0,{MAX_SLUG_LENGTH - 1}}}$"
#: The longest id: a built-in's, its slug behind `builtin:`.
MAX_MODEL_ID_LENGTH = len(BUILTIN_PREFIX) + MAX_SLUG_LENGTH

_SLUG_RE = re.compile(SLUG_PATTERN)
_SEPARATORS = re.compile(r"[^a-z0-9]+")


class InvalidSlugError(ValueError):
    pass


def is_slug(value: str) -> bool:
    """Is ``value`` a slug as :func:`slugify` makes them?"""
    return bool(_SLUG_RE.match(value)) and len(value) <= MAX_SLUG_LENGTH


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
