"""What the edge and the CRUD side's storefront routes share (#5282). It imports no
database code: the edge imports it, and the edge never imports `scadbuddy.orders`."""

from __future__ import annotations

import re
from typing import Final

#: A listing id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``listing_not_found`` before a lookup.
LISTING_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: An order item's id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``item_not_found`` before a lookup.
ITEM_ID: Final = LISTING_ID
#: A render job's id (``uuid4().hex``). Both sides answer any other 404
#: ``render_not_found`` before a lookup.
JOB_ID: Final = re.compile(r"[0-9a-f]{32}")
#: A design request's caps (spec §5.4): each file at most this many bytes, counted as
#: the bytes arrive, never from a header.
MAX_REQUEST_FILE_BYTES: Final = 25 * 1024 * 1024
#: The most files one request carries.
MAX_REQUEST_FILES: Final = 10
#: What a request's files may be, as their bytes say (``orders/attachments.py`` `sniff`):
#: photos, videos and PDFs. Anything else, an SVG or an HTML page among them, is refused.
REQUEST_KINDS: Final = frozenset({"image", "video", "pdf"})
#: The most characters of a request's text.
MAX_REQUEST_TEXT: Final = 5000
#: What `escape_filename` escapes, and `unescape_filename` decodes.
_ESCAPED: Final = re.compile(r"%(22|5C|[01][0-9A-F]|7F)", re.IGNORECASE)


def escape_filename(name: str) -> str:
    """A multipart ``filename`` value with ``"``, ``\\`` and control characters
    percent-escaped, as browsers escape ``"``, CR and LF (WHATWG HTML, "multipart/form-data
    encoding algorithm",
    https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#multipart-form-data).
    The CRUD side's parser (python-multipart 0.0.32 `parse_options_header`) ignores
    ``filename*``, as RFC 7578 §4.2 asks (https://www.rfc-editor.org/rfc/rfc7578#section-4.2),
    so the edge sends the name this way and the CRUD side decodes it
    (`unescape_filename`)."""
    return "".join(
        f"%{ord(c):02X}" if c in '"\\' or ord(c) < 32 or ord(c) == 127 else c for c in name
    )


def unescape_filename(sent: str) -> str:
    """`escape_filename` undone, and with it a browser's own escapes. A name that holds
    one of those escapes as text (``%22``) reads as the character, as a browser's would."""
    return _ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), sent)


#: The visitor id the edge verified in the cookie: 32 random bytes, URL-safe base64.
VISITOR_HEADER: Final = "X-ScadBuddy-Visitor"
#: The browser's address as the edge found it (`core/proxies.py` `client_address`).
CLIENT_HEADER: Final = "X-ScadBuddy-Store-Client"
#: Where the storefront's problem types sit, as `…/storefront/render_limited`.
STOREFRONT_PROBLEMS: Final = "https://scadbuddy.dev/problems/storefront/"
#: The CRUD side's refusals of the edge's own headers: a fault of the edge, not the
#: visitor's, so the edge never shows them.
VISITOR_ID_PROBLEM: Final = f"{STOREFRONT_PROBLEMS}visitor_id"
STORE_CLIENT_PROBLEM: Final = f"{STOREFRONT_PROBLEMS}store_client"
_INTERNAL_PROBLEMS: Final = frozenset({VISITOR_ID_PROBLEM, STORE_CLIENT_PROBLEM})


def public_problem(problem_type: object) -> bool:
    """Whether the edge may show a CRUD refusal of this type to the visitor: a storefront
    problem, other than the refusals of the edge's own headers."""
    return (
        isinstance(problem_type, str)
        and problem_type.startswith(STOREFRONT_PROBLEMS)
        and problem_type not in _INTERNAL_PROBLEMS
    )
