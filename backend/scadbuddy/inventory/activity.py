"""The inventory's operations on the Activities tab (#6949 K1, R-K1-22): one kind per Go
activity, as `ActivityIndex.ingest_inventory_operation` writes its rows (key the activity
name, `inventory.Take`), each with a link to its subject's page."""

from __future__ import annotations

from urllib.parse import quote

from scadbuddy.activity.models import ActivityKind, ActivityLink, ActivityRow

#: Each activity of `tools/go/core/internal/inventory/activities/register.go` and its label.
LABELS: dict[str, str] = {
    "KindCreate": "Add an item kind",
    "KindUpdate": "Edit an item kind",
    "KindArchive": "Archive an item kind",
    "KindRestore": "Restore an item kind",
    "ItemCreate": "Add an inventory item",
    "ItemUpdate": "Edit an inventory item",
    "ItemArchive": "Archive an inventory item",
    "ItemRestore": "Restore an inventory item",
    "ReceiveLot": "Receive a lot",
    "ReceiveUnits": "Receive units",
    "LotUpdate": "Edit a lot",
    "LotAdjust": "Count a lot",
    "Take": "Take stock",
    "Count": "Count an item",
    "UnitUpdate": "Edit a unit",
    "UnitStatus": "Change a unit's status",
    "GiveBack": "Give parts back",
    "Recost": "Re-cost a lot",
    "Repoint": "Move stock to a merged entity",
}

#: Each subject's page (`frontend/src/lib/inventoryLinks.ts`): a lot's sends its item's.
_PAGES = {
    "item": ("item", "Item", "/inventory/items/"),
    "lot": ("lot", "Lot", "/inventory/lots/"),
    "unit": ("unit", "Unit", "/inventory/units/"),
    "item_kind": ("kinds", "Item kinds", None),
}


def _links(row: ActivityRow) -> list[ActivityLink]:
    kind, _, ref = row.subject.partition(":")
    page = _PAGES.get(kind)
    if page is None or not ref:
        return []
    rel, label, base = page
    return [
        ActivityLink(rel, label, f"{base}{quote(ref, safe='')}" if base else "/inventory/kinds")
    ]


def title(kind: str, subject: str) -> str:
    """The row's title: the activity's label and its subject."""
    name = kind.removeprefix("inventory.")
    return f"{LABELS.get(name, name)} {subject}".strip()


ACTIVITY_KINDS = tuple(
    ActivityKind(key=f"inventory.{name}", label=label, links=_links)
    for name, label in LABELS.items()
)
