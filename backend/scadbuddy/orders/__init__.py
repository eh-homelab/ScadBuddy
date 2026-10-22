"""Orders (#4249, epic #4248; spec ``docs/superpowers/specs/2026-10-18-orders-design.md``):
customers, orders and line items as ScadBuddy's own records, and their prices (#4250).

``models.py`` is the wire and row shapes, ``store.py`` the rows, ``numbers.py`` the order
numbers, ``lifecycle.py`` every transition, ``component.py`` ``ORDERS``. The price book is
``pricebook.py`` and ``currency.py``, the arithmetic ``pricing.py``; a slice quote is the
``order_item_quote`` operation (``operations.py``), whose steps are ``quote.py``: the only
part that renders and slices, through the render service and the print dialog's own
background slice. Nothing here prints, and nothing imports from ``api/``.
"""
