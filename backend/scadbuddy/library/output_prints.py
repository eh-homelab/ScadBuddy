"""An output's last print, in Postgres (#1060): ``output_last_prints``.

Before #1060 a print wrote its Bambuddy ids into the output's ``meta.json``
(``OutputStore.record_send``), which only a process holding the data volume can do. The
print worker holds none, so the record is here, and `OutputStore` lays a row over what
an older ``meta.json`` says. Synchronous, like `OutputStore`: callers on the loop use a
thread.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.library.outputs import PlateSend, PrintRoute


class LastPrint(BaseModel):
    queue_item_id: int | None = None
    print_route: PrintRoute
    slice_job_id: int | None = None
    project_id: int | None = None
    plates: list[PlateSend] = Field(default_factory=list)

    def fields(self) -> dict[str, Any]:
        """What the row overrides on an ``OutputMeta``: a null id leaves the file's."""
        return {key: value for key, value in self.__dict__.items() if value is not None}


class OutputPrintStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def record(
        self,
        output_id: str,
        *,
        queue_item_id: int | None,
        slice_job_id: int | None,
        project_id: int | None,
        plates: list[PlateSend],
    ) -> None:
        """A slice-and-queue print's ids; a null id leaves the one already recorded, as
        ``record_send`` left an omitted field alone. ``plates`` is every plate so far."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_last_prints AS t"
                " (output_id, queue_item_id, print_route, slice_job_id, project_id, plates)"
                " VALUES (%s, %s, 'slice_queue', %s, %s, %s)"
                " ON CONFLICT (output_id) DO UPDATE SET"
                " queue_item_id = COALESCE(EXCLUDED.queue_item_id, t.queue_item_id),"
                " print_route = EXCLUDED.print_route,"
                " slice_job_id = COALESCE(EXCLUDED.slice_job_id, t.slice_job_id),"
                " project_id = COALESCE(EXCLUDED.project_id, t.project_id),"
                " plates = EXCLUDED.plates, recorded_at = now()",
                (
                    output_id,
                    queue_item_id,
                    slice_job_id,
                    project_id,
                    Jsonb([plate.model_dump(mode="json") for plate in plates]),
                ),
            )

    def for_outputs(self, output_ids: Sequence[str]) -> dict[str, LastPrint]:
        if not output_ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT output_id, queue_item_id, print_route, slice_job_id, project_id, plates"
                " FROM output_last_prints WHERE output_id = ANY(%s)",
                (list(output_ids),),
            ).fetchall()
        return {row["output_id"]: LastPrint.model_validate(row) for row in rows}

    def delete(self, output_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_last_prints WHERE output_id = %s", (output_id,))
