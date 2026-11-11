"""What the edge and the CRUD side's storefront routes share (#5282). It imports no
database code: the edge imports it, and the edge never imports `scadbuddy.orders`."""

from __future__ import annotations

import re
from typing import Final, Literal

#: A listing id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``listing_not_found`` before a lookup.
LISTING_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: An order item's id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``item_not_found`` before a lookup.
ITEM_ID: Final = LISTING_ID
#: A design revision's id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``revision_not_found`` before a lookup.
REVISION_ID: Final = LISTING_ID
#: A quote's id: ``gen_random_uuid()::text``, or a quote run's ``uuid.hex``
#: (`orders/quote.py` `operation_quote_id`). Both sides answer any other 404
#: ``quote_not_found`` before a lookup.
QUOTE_ID: Final = re.compile(rf"{LISTING_ID.pattern}|[0-9a-f]{{32}}")
#: Every field of the customer's order view (`orders/storefront/wire.py`
#: `CustomerOrderView`), by its dotted path from the view: the edge sends on these only
#: (`storefront/routes.py`), so no cost, margin, internal note, shop event or staff name
#: reaches a customer. A field added to the view is added here too, or
#: `tests/orders/storefront/test_order_page.py` fails.
ORDER_VIEW_FIELDS: Final[dict[str, frozenset[str]]] = {
    "": frozenset(
        {
            "number",
            "status",
            "currency",
            "minor_exponent",
            "can_cancel",
            "items",
            "payments",
            "shipments",
        }
    ),
    "items": frozenset({"id", "title", "quantity", "status", "revisions", "quote"}),
    "items.revisions": frozenset(
        {
            "id",
            "number",
            "note",
            "sent_at",
            "answer",
            "answer_note",
            "can_answer",
            "pictures",
            "colours",
        }
    ),
    "items.revisions.pictures": frozenset({"url", "kind", "caption"}),
    "items.revisions.colours": frozenset({"slot", "colour", "name"}),
    "items.quote": frozenset(
        {"id", "unit_price_minor", "quantity", "total_minor", "accepted", "can_accept"}
    ),
    "payments": frozenset({"id", "kind", "amount_minor", "currency", "status", "pay_url"}),
    "shipments": frozenset({"carrier", "service", "tracking", "shipped_at"}),
}
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
#: A signed-in visitor's session token, from the edge's verified ``sb_session`` cookie
#: (`storefront/accounts.py`). The CRUD side's verify sends a new one back in it too.
SESSION_HEADER: Final = "X-ScadBuddy-Store-Session"
_INTERNAL_PROBLEMS: Final = frozenset({VISITOR_ID_PROBLEM, STORE_CLIENT_PROBLEM})
#: The forms that take the bot check (spec §5.8), each the widget's ``action``: a token
#: made for one form does not pass another's check. Each has a route.
TurnstileAction = Literal["submit", "request", "sign_up", "sign_in"]
#: The header the page sends the widget's token in.
TURNSTILE_HEADER: Final = "X-Turnstile-Token"
#: Cloudflare's bound on a token's length
#: (https://developers.cloudflare.com/turnstile/get-started/server-side-validation/).
MAX_TURNSTILE_TOKEN_CHARS: Final = 2048
#: How long the CRUD side's one Siteverify call may take, connect to answer.
SITEVERIFY_SECONDS: Final = 10.0
#: How long the edge waits for the CRUD side's verdict: the Siteverify call, plus room.
TURNSTILE_VERIFY_SECONDS: Final = SITEVERIFY_SECONDS + 5.0


def public_problem(problem_type: object) -> bool:
    """Whether the edge may show a CRUD refusal of this type to the visitor: a storefront
    problem, other than the refusals of the edge's own headers."""
    return (
        isinstance(problem_type, str)
        and problem_type.startswith(STOREFRONT_PROBLEMS)
        and problem_type not in _INTERNAL_PROBLEMS
    )
