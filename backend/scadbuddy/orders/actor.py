"""Who a request acts as (#4249, plan Ruling 12): the agent principal the author header
names (``core/authorship.py``), else the browser. Never from a body."""

from __future__ import annotations

from scadbuddy.core.authorship import current_author
from scadbuddy.orders.models import Actor


def current_actor() -> Actor:
    """As ``api/flows.py`` ``_started_by`` reads it."""
    author = current_author()
    if author is None:
        return Actor(kind="browser")
    return Actor(kind="agent", principal=author.principal, session=author.session)
