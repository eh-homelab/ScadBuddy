"""The sweep of entities whose owner is gone (#6950, spec D4, phase 1 form): the
housekeeping step `ENTITY_ORPHANS_SWEEP` runs it. Owner stores do not drop their
entities yet; later phases add that call in each store they own, and this sweep stays
as the backstop."""

from __future__ import annotations

from scadbuddy.entities.attachments import ORDER_KINDS, EntityAttachmentStore
from scadbuddy.entities.registry import EntityStore


def sweep_orphan_entities(
    entities: EntityStore, attachments: EntityAttachmentStore, *, limit: int = 500
) -> int:
    """Up to `limit` orphans removed, each in one transaction: its attachments (and
    their holds) first, since `entity_attachments.entity_id` restricts the delete, then
    the row, which takes its links. An order's kinds are left to the order code: their
    attachments also restrict on `order_id`, and the order's delete removes them."""
    removed = 0
    with entities.pool.connection() as conn:
        for entity_id, _ref in entities.orphans(conn, limit, skip=ORDER_KINDS):
            with conn.transaction():
                rows = conn.execute(
                    "SELECT id FROM entity_attachments WHERE entity_id = %s FOR UPDATE",
                    (entity_id,),
                ).fetchall()
                for row in rows:
                    attachments.delete(conn, row["id"])
                deleted = conn.execute("DELETE FROM entities WHERE id = %s", (entity_id,)).rowcount
            removed += deleted
    return removed
