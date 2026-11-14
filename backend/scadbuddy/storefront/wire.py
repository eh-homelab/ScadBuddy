"""What the edge and the CRUD side's storefront routes share (#5282). It imports no
database code: the edge imports it, and the edge never imports `scadbuddy.orders`."""

from __future__ import annotations

import re
from typing import Final, Literal

#: A listing id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``listing_not_found`` before a lookup.
LISTING_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: The most cards one page of the public list holds (#6154).
LISTINGS_MAX_PAGE: Final = 48
#: The longest title search the public list takes.
LISTINGS_MAX_QUERY: Final = 100
#: A title search holds no NUL: Postgres text cannot, and the bind would fail (500).
LISTINGS_QUERY_PATTERN: Final = r"^[^\x00]*$"
#: An order item's id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``item_not_found`` before a lookup.
ITEM_ID: Final = LISTING_ID
#: A customer's own models (#6133, spec 2026-11-13 §6.1). Each file goes straight to the
#: store's staging area by a presigned PUT, at most `MAX_MODEL_FILE_BYTES` (256 MiB: 2.7
#: times the largest real MakerWorld 3MF seen, 98 MB), at most `MAX_MODEL_FILES` files and
#: `MAX_PRINT_LINKS` links a request and `MAX_MODEL_REQUEST_BYTES` declared in all; at most
#: `MAX_PURCHASE_LINKS` things to buy per item (the owner's cap).
MAX_MODEL_FILE_BYTES: Final = 256 * 1024 * 1024
MAX_MODEL_FILES: Final = 20
MAX_PRINT_LINKS: Final = 10
MAX_PURCHASE_LINKS: Final = 5
MAX_MODEL_REQUEST_BYTES: Final = 1024 * 1024 * 1024
#: The longest file name of an upload slot, and of a link's note, in characters.
MAX_UPLOAD_NAME: Final = 200
MAX_LINK_NOTE: Final = 500
#: The longest link a customer gives, in characters (`orders/customer_links.py`).
MAX_LINK_CHARS: Final = 2048
#: A design request's things to buy (#6133, spec 2026-11-13 §10.1): the multipart text
#: part that holds them, a JSON list of ``{"url", "note"}``, at most
#: `MAX_PURCHASE_LINKS`, and that part's most bytes, with room for the framing. Each
#: character of a link and a note is at most six bytes: UTF-8 is at most four, and JSON
#: escapes a control character as ``\u00XX``. The edge forwards the part as UTF-8
#: (`crud/storefront.py` `_multipart`, ``ensure_ascii=False``); ASCII escapes would write
#: a character outside the BMP as a surrogate pair, 12 bytes.
PURCHASE_LINKS_FIELD: Final = "purchase_links"
MAX_PURCHASE_LINKS_BYTES: Final = MAX_PURCHASE_LINKS * (MAX_LINK_CHARS + MAX_LINK_NOTE) * 6 + 1024
#: The most colours a customer may ask for (the price book's filaments, by id).
MAX_COLOUR_CHOICES: Final = 8
#: An upload slot's id (``uuid4().hex``). Both sides refuse any other before a lookup.
UPLOAD_ID: Final = re.compile(r"[0-9a-f]{32}")
#: A design revision's id (``gen_random_uuid()::text``). Both sides answer any other 404
#: ``revision_not_found`` before a lookup.
REVISION_ID: Final = LISTING_ID
#: A quote's id: ``gen_random_uuid()::text``, or a quote run's ``uuid.hex``
#: (`orders/quote.py` `operation_quote_id`). Both sides answer any other 404
#: ``quote_not_found`` before a lookup.
QUOTE_ID: Final = re.compile(rf"{LISTING_ID.pattern}|[0-9a-f]{{32}}")
#: The fields of the pay page's checkout (`payments/venmo.py` `VenmoCheckout`) the edge
#: sends on; nothing else the CRUD side sent reaches the browser.
PAY_CHECKOUT_FIELDS: Final = frozenset(
    {
        "amount_minor",
        "currency",
        "order_number",
        "client_id",
        "paypal_order_id",
        "minor_exponent",
    }
)
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
            "payment_instructions",
            "shipments",
            "charges",
        }
    ),
    "items": frozenset({"id", "title", "quantity", "status", "revisions", "quote", "sources"}),
    "items.sources": frozenset(
        {
            "kind",
            "purpose",
            "filename",
            "url",
            "note",
            "status",
            "amount_minor",
            "shop_note",
            "preview_url",
            "customer_reason",
        }
    ),
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
    "payments": frozenset(
        {"id", "kind", "amount_minor", "currency", "status", "pay_url", "manual"}
    ),
    "shipments": frozenset({"carrier", "service", "tracking", "shipped_at"}),
    "charges": frozenset({"kind", "label", "amount_minor"}),
}
#: An order item source's id (``gen_random_uuid()::text``, #6133). Both sides answer any
#: other with a bare 404 before a lookup.
SOURCE_ID: Final = LISTING_ID
#: The headers of a checked file's preview, wherever it is served (#6133, spec 2026-11-13
#: §7 "Serving the preview"). The bytes are the untrusted check's and are never decoded or
#: re-encoded, so they are always a PNG shown inline that no browser sniffs as anything
#: else. A presigned GET cannot set ``nosniff``, so the bytes always come through a route.
#: Only the reader's own browser keeps a copy, and only for a short time: a check run
#: again stores a new preview.
PREVIEW_HEADERS: Final[dict[str, str]] = {
    "Content-Type": "image/png",
    "Content-Disposition": "inline",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=60",
}


def draft_preview_path(source_id: str) -> str:
    """The edge's path of a cart line's preview (the visitor's cookie reads it)."""
    return f"/api/store/v1/draft/sources/{source_id}/preview"


def order_preview_path(source_id: str) -> str:
    """The edge's path of an order source's preview. The page POSTs its reader's body to
    it (the order link's token, or a signed-in reader's ``order_id``), so no URL holds the
    token."""
    return f"/api/store/v1/order/sources/{source_id}/preview"


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
TurnstileAction = Literal["submit", "request", "model", "sign_up", "sign_in"]
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
