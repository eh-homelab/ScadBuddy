"""The orders' activity kinds (#4250, spec 2026-10-09 §4): a slice quote is an
operation, so its row is ``operations/activity.py``'s; this names its kind."""

from __future__ import annotations

from scadbuddy.activity.kinds import operation_kind_key
from scadbuddy.activity.models import ActivityKind

ORDER_QUOTE = ActivityKind(key=operation_kind_key("order_item_quote"), label="Quote from a slice")
ACTIVITY_KINDS = (ORDER_QUOTE,)
