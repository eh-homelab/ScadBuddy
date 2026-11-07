"""What the edge and the CRUD side's storefront routes share (#5282). It imports no
database code: the edge imports it, and the edge never imports `scadbuddy.orders`."""

from __future__ import annotations

from typing import Final

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
