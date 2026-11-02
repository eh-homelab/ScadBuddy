"""The orders' activity kinds (#4250, spec 2026-10-09 §4): a slice quote, an
attachment's upload (#4251) and an item's print (#4719) are operations, so their rows are
``operations/activity.py``'s; this names their kinds."""

from __future__ import annotations

from scadbuddy.activity.kinds import operation_kind_key
from scadbuddy.activity.models import ActivityKind

ORDER_QUOTE = ActivityKind(key=operation_kind_key("order_item_quote"), label="Quote from a slice")
ORDER_ATTACH = ActivityKind(
    key=operation_kind_key("order_attachment_add"), label="Attach a file to an order"
)
ORDER_PRINT = ActivityKind(
    key=operation_kind_key("order_item_print"), label="Print an order's line item"
)
ACTIVITY_KINDS = (ORDER_QUOTE, ORDER_ATTACH, ORDER_PRINT)
