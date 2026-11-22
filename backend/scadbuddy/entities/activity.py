"""The entities' activity kinds (`activity/kinds.py`): one per operation kind."""

from __future__ import annotations

from scadbuddy.activity.kinds import operation_kind_key
from scadbuddy.activity.models import ActivityKind

ENTITY_ATTACH = ActivityKind(key=operation_kind_key("entity_attachment_add"), label="Attach a file")
ACTIVITY_KINDS = (ENTITY_ATTACH,)
