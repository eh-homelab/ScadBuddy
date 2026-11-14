"""A storefront customer's links (#6133, spec 2026-11-13 §9).

`checked_link` holds a link to text rules only: at most `MAX_LINK_CHARS` characters, no
whitespace or control character, scheme ``http`` or ``https``, a host, no user name or
password, the host in IDNA ASCII form, lower-cased. Nothing here fetches, resolves or
follows a link: the CRUD service stores the text.

`link_type` types a ``print`` link by the first handler of `HANDLERS` that matches it: the
scheme ``https``, a host in the handler's exact set, the default port, and the path in
full against the handler's pattern (the query and fragment are not matched, and stay in
the stored text). No match is ``manual``, for the back office to handle by hand. A
``purchase`` link is always ``manual``: the shop buys by hand. A MakerWorld link is
imported through Bambuddy's MakerWorld import (#6128); the other types only tell the
shop where the link points.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from scadbuddy.storefront.wire import MAX_LINK_CHARS as WIRE_MAX_LINK_CHARS

LinkType = Literal["makerworld", "printables", "thingiverse", "manual"]
LinkPurpose = Literal["print", "purchase"]

#: The longest link taken, in characters: the edge's cap (`storefront/wire.py`).
MAX_LINK_CHARS: Final = WIRE_MAX_LINK_CHARS


class LinkInvalidError(ValueError):
    """A link that breaks a rule of `checked_link`, with a sentence a customer may read."""


@dataclass(frozen=True)
class LinkHandler:
    type: LinkType
    hosts: frozenset[str]
    path: re.Pattern[str]


#: The handlers, first match wins. The paths are the sites' public model links.
HANDLERS: Final[tuple[LinkHandler, ...]] = (
    LinkHandler(
        "makerworld",
        frozenset({"makerworld.com", "www.makerworld.com"}),
        re.compile(r"/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?models/(\d{1,12})(?:-[^/]*)?/?"),
    ),
    LinkHandler(
        "printables",
        frozenset({"printables.com", "www.printables.com"}),
        re.compile(r"/(?:[a-z]{2}/)?model/(\d{1,12})(?:-[^/]*)?/?"),
    ),
    LinkHandler(
        "thingiverse",
        frozenset({"thingiverse.com", "www.thingiverse.com"}),
        re.compile(r"/thing:(\d{1,12})/?"),
    ),
)


def _refused(reason: str) -> LinkInvalidError:
    return LinkInvalidError(f"This link cannot be used: {reason}.")


def _parts(url: str) -> SplitResult:
    try:
        parts = urlsplit(url)
        # Reading the port checks it is a number in range.
        parts.port  # noqa: B018
    except ValueError:
        raise _refused("it is not a web address") from None
    return parts


def checked_link(url: str) -> str:
    """``url`` as stored: its host lower-cased in IDNA ASCII form, the rest as sent; else
    `LinkInvalidError`."""
    if not url or len(url) > MAX_LINK_CHARS:
        raise _refused(f"it must be 1 to {MAX_LINK_CHARS} characters")
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in url):
        raise _refused("it holds a space or a control character")
    parts = _parts(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise _refused("it must start with http:// or https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _refused("it holds a user name or password")
    host = parts.hostname
    if not host:
        raise _refused("it names no site")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _refused("its site name is not valid") from None
    netloc = ascii_host if parts.port is None else f"{ascii_host}:{parts.port}"
    stored = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
    if len(stored) > MAX_LINK_CHARS:
        raise _refused(f"it must be 1 to {MAX_LINK_CHARS} characters")
    return stored


def link_type(url: str, purpose: LinkPurpose) -> tuple[LinkType, str | None]:
    """The type of a link `checked_link` took, and its canonical id (a model's or a
    thing's number), or ``("manual", None)``."""
    if purpose == "purchase":
        return "manual", None
    parts = _parts(url)
    if parts.scheme != "https" or parts.port is not None:
        return "manual", None
    for handler in HANDLERS:
        if parts.hostname in handler.hosts:
            found = handler.path.fullmatch(parts.path)
            if found is not None:
                return handler.type, found.group(1)
    return "manual", None
