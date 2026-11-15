"""`storefront_mail_covers` needs a ready mail config: a host and a sender (no database)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from scadbuddy.orders.storefront.mail import MailConfig, storefront_mail_covers


@pytest.mark.parametrize(
    ("host", "sender"), [("smtp.example", None), (None, "shop@example.com"), (None, None)]
)
def test_covers_is_false_unless_the_host_and_the_sender_are_set(
    host: str | None, sender: str | None
) -> None:
    """The check stops before it asks the database, so none is given."""
    config = MailConfig(host=host, port=587, user=None, sender=sender)
    assert storefront_mail_covers(cast(Any, None), "o1", "shipped", ready=config.ready) is False
