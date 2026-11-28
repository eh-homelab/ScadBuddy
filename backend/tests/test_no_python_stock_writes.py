"""Python writes no stock (#6949 K1 Task 16, design §6.1a): Go is the one writer of the
kind, item and stock tables. A scan of `backend/scadbuddy/**/*.py` (not the migrations)
finds no statement that inserts into, updates or deletes from one of them. The only file
allowed a hit is `inventory/offers.py`, whose tables (`supplier_offers`,
`offer_price_points`) are not stock tables (K3)."""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "scadbuddy"
TABLES = ("items", "item_kinds", "stock_lots", "stock_units", "stock_moves")
WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:" + "|".join(TABLES) + r")\b",
    re.IGNORECASE,
)
ALLOWED = {PACKAGE / "inventory" / "offers.py"}


def _hits() -> list[str]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "migrations" in path.relative_to(PACKAGE).parts[:1] or path in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for match in WRITE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(PACKAGE)}:{line}: {match.group(0)}")
    return found


def test_no_python_stock_writes() -> None:
    assert _hits() == []


def test_the_scan_finds_a_write() -> None:
    """The pattern matches each form of write it looks for, and not another table."""
    for sql in (
        "INSERT INTO stock_moves (id) VALUES (1)",
        "UPDATE stock_lots SET on_hand = 0",
        "delete from items where id = 1",
        'f"UPDATE {x}" or "UPDATE item_kinds SET name = 1"',
        "UPDATE\n stock_units SET printer_id = 1",
    ):
        assert WRITE.search(sql), sql
    for sql in ("UPDATE order_items SET x = 1", "SELECT * FROM stock_moves"):
        assert not WRITE.search(sql), sql
