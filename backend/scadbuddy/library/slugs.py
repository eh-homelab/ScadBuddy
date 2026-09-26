from __future__ import annotations

import re

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
MAX_SLUG_LENGTH = 100
#: A template's id: ``<slug>`` for mine, ``builtin:<slug>`` for a built-in. ``:`` is
#: not a slug character, so the two namespaces cannot collide and every id minted
#: before built-ins existed keeps meaning what it meant.
BUILTIN_PREFIX = "builtin:"
MODEL_ID_PATTERN = r"^(builtin:)?[a-z0-9][a-z0-9-]*$"

_SLUG_RE = re.compile(SLUG_PATTERN)
_SEPARATORS = re.compile(r"[^a-z0-9]+")


class InvalidSlugError(ValueError):
    pass


def is_builtin(model_id: str) -> bool:
    return model_id.startswith(BUILTIN_PREFIX)


def builtin_id(slug: str) -> str:
    return f"{BUILTIN_PREFIX}{slug}"


def bare_slug(model_id: str) -> str:
    """The slug without its namespace: what a filename or a default name is made of."""
    return model_id.removeprefix(BUILTIN_PREFIX)


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
