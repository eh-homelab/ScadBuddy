"""Who made a spool event, beyond its ``actor`` (#6269, plan Decision 10): the agent or flow
a call came from, as ``data.author`` on every event `ledger.record` books."""

from __future__ import annotations

from scadbuddy.core.authorship import current_author
from scadbuddy.orders.actor import current_actor


def current_by() -> dict[str, str] | None:
    """The agent or flow the current request is made by, as a plain dict of the keys that
    are set: ``principal``, ``session``, ``flow_run`` and ``tool``; ``None`` for a browser.

    It derives from `orders/actor.py` `current_actor()`, so the ledger and the orders name
    the same caller, but it is not that ``Actor`` itself: the ledger stores a plain JSON
    dict, ``Actor`` has no ``tool`` (it comes from `current_author()`), and ``Actor``'s
    ``run_id`` is stored as ``flow_run``, the header's name."""
    found = current_actor()
    if found.kind == "browser":
        return None
    author = current_author()
    values = {
        "principal": found.principal,
        "session": found.session,
        "flow_run": found.run_id,
        "tool": None if author is None else author.tool,
    }
    return {key: value for key, value in values.items() if value is not None}
