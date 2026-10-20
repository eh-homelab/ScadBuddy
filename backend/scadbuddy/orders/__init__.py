"""Orders (#4249, epic #4248; spec ``docs/superpowers/specs/2026-10-18-orders-design.md``):
customers, orders and line items as ScadBuddy's own records.

``models.py`` is the wire and row shapes, ``store.py`` the rows, ``numbers.py`` the order
numbers, ``lifecycle.py`` every transition, ``component.py`` ``ORDERS``. Nothing here
renders, slices, prints or reaches Bambuddy, and nothing imports from ``api/``.
"""
