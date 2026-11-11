"""Registry printers for tests that remember something per printer (#5153)."""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.printers.component import PRINTERS, Printers


def bambuddy_printer(conninfo: str, bambuddy_id: int) -> str:
    """The registry printer that links Bambuddy printer ``bambuddy_id``, made when the
    registry holds none: its id. A plate is remembered on a registry printer only."""
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), %s, %s, %s)"
            " ON CONFLICT (bambuddy_printer_id) DO UPDATE SET name = printers.name"
            " RETURNING id::text",
            (f"TEST{bambuddy_id:011d}", f"Printer {bambuddy_id}", bambuddy_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def linked_printer(client: TestClient, bambuddy_id: int) -> str:
    """The app's registry printer that links Bambuddy printer ``bambuddy_id``, made as a
    print run makes it (`PrinterStore.ensure_bambuddy`): its id."""
    registry: Printers = getattr(client.app.state, STATE_ATTR).components.get(PRINTERS)  # type: ignore[attr-defined]
    row = registry.store.ensure_bambuddy(
        bambuddy_id, serial=None, name=f"Printer {bambuddy_id}", model=None
    )
    return row.id
