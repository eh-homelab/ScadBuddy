"""An order's links for the customer (#5283, plan Tasks 3 and 5): ``/o#<token>``.

A token is 32 random bytes, URL-safe base64. The row keeps its sha256 (hex), which finds
the link, and the token sealed under the CRUD service's KEK (AAD
``storefront_order_links:<id>``), which only the mail sender opens, to write the link into
a mail (ruling R1). Revoking a link drops its sealed token. These run in the caller's
transaction.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from scadbuddy.core.secrets import Envelope, Kek, open_secret, seal_secret
from scadbuddy.orders.store import Conn
from scadbuddy.orders.storefront.addresses import check_address
from scadbuddy.payments.credentials import KekMissingError

TOKEN_BYTES = 32


def token_aad(link_id: str) -> str:
    return f"storefront_order_links:{link_id}"


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class LiveLink:
    id: str
    sent_to: str


def create_order_link(conn: Conn, kek: Kek | None, order_id: str, to: str) -> tuple[str, str]:
    """A new live link of the order for ``to`` (one address, `check_address`): its id and
    its token. The token is returned once, for the caller's answer; it is stored only as
    its sha256 and sealed."""
    if kek is None:
        raise KekMissingError()
    to = check_address(to)
    link_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(TOKEN_BYTES)
    sealed = seal_secret(kek, token, token_aad(link_id))
    conn.execute(
        "INSERT INTO storefront_order_links (id, order_id, token_sha256, token_sealed,"
        " dek_sealed, kek_id, sent_to) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            link_id,
            order_id,
            token_sha256(token),
            sealed.secret_sealed,
            sealed.dek_sealed,
            sealed.kek_id,
            to,
        ),
    )
    return link_id, token


def revoke_order_links(conn: Conn, order_id: str) -> int:
    """Revoke every live link of the order, dropping each one's sealed token. How many."""
    return conn.execute(
        "UPDATE storefront_order_links SET revoked_at = clock_timestamp(), token_sealed = NULL,"
        " dek_sealed = NULL, kek_id = NULL WHERE order_id = %s AND revoked_at IS NULL",
        (order_id,),
    ).rowcount


def live_link(conn: Conn, order_id: str) -> LiveLink | None:
    """The order's newest live link: where its mail goes."""
    row = conn.execute(
        "SELECT id, sent_to FROM storefront_order_links WHERE order_id = %s"
        " AND revoked_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 1",
        (order_id,),
    ).fetchone()
    return None if row is None else LiveLink(id=str(row["id"]), sent_to=str(row["sent_to"]))


def sealed_token(conn: Conn, link_id: str) -> Envelope | None:
    """The link's sealed token, or None once it is revoked (or gone)."""
    row = conn.execute(
        "SELECT token_sealed, dek_sealed, kek_id FROM storefront_order_links"
        " WHERE id = %s AND revoked_at IS NULL AND token_sealed IS NOT NULL",
        (link_id,),
    ).fetchone()
    if row is None:
        return None
    return Envelope(
        secret_sealed=bytes(row["token_sealed"]),
        dek_sealed=bytes(row["dek_sealed"]),
        kek_id=row["kek_id"],
    )


def open_token(kek: Kek, link_id: str, envelope: Envelope) -> str:
    """The link's token. Raises `core.secrets.SealError` under another key."""
    return open_secret(kek, envelope, token_aad(link_id))
