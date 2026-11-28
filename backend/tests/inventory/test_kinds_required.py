"""R-K1-33: an item's required keys are its kinds' merged `required` and then its special
type's `requires_properties`, as Go's `itemSchema`."""

from __future__ import annotations

import json
from pathlib import Path

from scadbuddy.entities.properties import schema_for
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.required import completeness_of_kind
from scadbuddy.inventory.kinds import SPECIAL_REQUIRES, merged_schema
from tests.conftest import PgPool
from tests.support.inventory_rows import insert_item, insert_kind

GO_SPECIAL_REQUIRES = (
    Path(__file__).resolve().parents[3]
    / "tools/go/core/internal/inventory/store/testdata/special_requires.json"
)


def test_the_special_types_requires_are_gos() -> None:
    """Go's catalog has the same `requires_properties` (its test reads the same file)."""
    assert json.loads(GO_SPECIAL_REQUIRES.read_text())["special_types"] == SPECIAL_REQUIRES


def test_an_items_required_keys_add_its_special_types(pg_pool: PgPool) -> None:
    pla = insert_kind(
        pg_pool,
        parent_id="filament",
        tracking="none",
        attribute_schema={
            "type": "object",
            "properties": {"td": {"type": "number"}},
            "required": ["td", "rgba"],
        },
    )
    item = str(insert_item(pg_pool, pla, name="PLA Basic"))
    with pg_pool.connection() as conn:
        # The kind's schema keeps its own keys; the item's adds the special type's.
        assert merged_schema(conn, pla)["required"] == ["td", "rgba"]
        assert schema_for(conn, EntityRef("item", item))["required"] == [
            "td",
            "rgba",
            "diameter_mm",
        ]
        found = completeness_of_kind(conn, "item", [item])[item]
    assert found.missing == ("td", "rgba", "diameter_mm")


def test_a_kind_with_no_special_type_requires_only_its_schemas_keys(pg_pool: PgPool) -> None:
    item = str(insert_item(pg_pool, "hardware", name="M3 nut"))
    with pg_pool.connection() as conn:
        assert "required" not in schema_for(conn, EntityRef("item", item))
        assert completeness_of_kind(conn, "item", [item])[item].complete
