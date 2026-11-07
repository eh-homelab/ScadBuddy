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
