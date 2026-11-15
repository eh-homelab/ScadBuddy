"""One mail address, as the storefront's mail takes it (#5283, review I1).

A recipient is exactly one addr-spec: no display name, no list, no line break. Anything
else could put a second recipient (and with it a sign-in code or an order link) into the
envelope, so it is refused as `EmailInvalidError` (422 ``email_invalid`` at a route).
"""

from __future__ import annotations

import re
from email.utils import getaddresses, parseaddr

#: One ``@``, a dot in the domain, no space, comma, quote or angle bracket.
_ADDR_SPEC = re.compile(r"[^@\s,;<>\"()]+@[^@\s,;<>\"()]+\.[^@\s,;<>\"()]+")
MAX_LENGTH = 254


class EmailInvalidError(ValueError):
    slug = "email_invalid"


def check_address(to: str) -> str:
    """``to`` as one bare address, or `EmailInvalidError`."""
    if not isinstance(to, str) or any(c in to for c in "\r\n,;"):
        raise EmailInvalidError("email: one address, such as ada@example.com")
    address = to.strip()
    name, spec = parseaddr(address)
    if (
        len(address) > MAX_LENGTH
        or name
        or spec != address
        or len(getaddresses([address])) != 1
        or not _ADDR_SPEC.fullmatch(address)
    ):
        raise EmailInvalidError("email: one address, such as ada@example.com")
    return address
