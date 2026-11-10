"""The customer actor (#5283, spec 2026-11-02 §6.4): what a customer does on their
submitted storefront order, recorded as ``customer:<customer id>`` with the visitor's (or,
once signed in, the account's) row id as the session. Only the CRUD side builds it, from
what the storefront's service credential vouches for, never from a request body."""

from __future__ import annotations

from scadbuddy.orders.models import Actor


def customer_actor(customer_id: str, session: str) -> Actor:
    """The actor of the customer's accepts and answers on their order."""
    return Actor(kind="customer", principal=f"customer:{customer_id}", session=session)
