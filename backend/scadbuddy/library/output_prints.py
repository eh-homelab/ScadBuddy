"""An output's last print, in Postgres (#1060): ``output_last_prints``.

Before #1060 a print wrote its Bambuddy ids into the output's ``meta.json``
(``OutputStore.record_send``), which only a process holding the data volume can do. The
print worker holds none, so the record is here, and `OutputStore` lays a row over what
an older ``meta.json`` says. Synchronous, like `OutputStore`: callers on the loop use a
thread.

A print over the LAN (#4644) has no Bambuddy ids: its row names its run, its registry
printer and each plate with its copies (`record_lan`), and the native follow moves its
stage as it reads the print (`record_lan_progress`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.stages import Stage
from scadbuddy.library.outputs import LanPlateSend, PlateSend, PrintRoute

#: The fields a row lays over the file's even when ``None``: the last print's project
#: is its own, ``None`` for a folder or no project (#3790 review). Every other null
#: id leaves the file's. A row written before then with a null project lays it over an
#: older ``meta.json``'s project too, as intended: the row is always the newer print.
ALWAYS_OVERLAID = frozenset({"project_id"})
#: A LAN print's row lays its null Bambuddy ids over the file's too (#4644): it printed
#: on no queue, so an older ``meta.json``'s queue item is not its.
LAN_OVERLAID = ALWAYS_OVERLAID | {"queue_item_id", "slice_job_id"}
#: What only a LAN print's row says (#4644).
LAN_FIELDS = frozenset({"print_run_id", "printer", "lan_plates", "print_stage", "print_settled"})


class LastPrint(BaseModel):
    queue_item_id: int | None = None
    print_route: PrintRoute
    slice_job_id: int | None = None
    project_id: int | None = None
    plates: list[PlateSend] = Field(default_factory=list)
    print_run_id: str | None = None
    printer: str | None = None
    lan_plates: list[LanPlateSend] = Field(default_factory=list)
    print_stage: Stage | None = None
    print_settled: bool = False

    def fields(self) -> dict[str, Any]:
        """What the row overrides on an ``OutputMeta``: a null id leaves the file's,
        except the fields in :data:`ALWAYS_OVERLAID` (:data:`LAN_OVERLAID` for a LAN
        print's row). The :data:`LAN_FIELDS` are a LAN print's only."""
        lan = self.print_route == "lan"
        overlaid = LAN_OVERLAID if lan else ALWAYS_OVERLAID
        return {
            key: value
            for key, value in self.__dict__.items()
            if (lan or key not in LAN_FIELDS) and (value is not None or key in overlaid)
        }


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
        ``record_send`` left an omitted field alone. The project is the run's, every plate
        alike, so ``None`` (a print into a library folder or no project) clears an
        earlier one rather than leaving it the output's default (#3790 review).
        ``plates`` is every plate so far. An earlier LAN print's fields are cleared."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_last_prints AS t"
                " (output_id, queue_item_id, print_route, slice_job_id, project_id, plates)"
                " VALUES (%s, %s, 'slice_queue', %s, %s, %s)"
                " ON CONFLICT (output_id) DO UPDATE SET"
                " queue_item_id = COALESCE(EXCLUDED.queue_item_id, t.queue_item_id),"
                " print_route = EXCLUDED.print_route,"
                " slice_job_id = COALESCE(EXCLUDED.slice_job_id, t.slice_job_id),"
                " project_id = EXCLUDED.project_id,"
                " plates = EXCLUDED.plates,"
                " print_run_id = NULL, printer = NULL, lan_plates = '[]',"
                " print_stage = NULL, print_settled = false,"
                " recorded_at = now()",
                (
                    output_id,
                    queue_item_id,
                    slice_job_id,
                    project_id,
                    Jsonb([plate.model_dump(mode="json") for plate in plates]),
                ),
            )

    def record_lan(
        self,
        output_id: str,
        *,
        run_id: str,
        printer: str | None,
        plates: list[LanPlateSend],
    ) -> None:
        """A LAN print (#4644): its run, its registry printer and each plate with its
        copies, printing as the printer took its first start. A Bambuddy print's ids
        recorded before are cleared. Recorded again for the same run (a retried record)
        it keeps the stage the follow has written since."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_last_prints AS t"
                " (output_id, print_route, print_run_id, printer, lan_plates, print_stage)"
                " VALUES (%s, 'lan', %s, %s, %s, 'running')"
                " ON CONFLICT (output_id) DO UPDATE SET"
                " queue_item_id = NULL, print_route = 'lan', slice_job_id = NULL,"
                " project_id = NULL, plates = '[]',"
                " print_stage = CASE WHEN t.print_run_id = EXCLUDED.print_run_id"
                "  THEN t.print_stage ELSE EXCLUDED.print_stage END,"
                " print_settled = t.print_run_id IS NOT DISTINCT FROM EXCLUDED.print_run_id"
                "  AND t.print_settled,"
                " print_run_id = EXCLUDED.print_run_id, printer = EXCLUDED.printer,"
                " lan_plates = EXCLUDED.lan_plates, recorded_at = now()",
                (
                    output_id,
                    run_id,
                    printer,
                    Jsonb([plate.model_dump(mode="json") for plate in plates]),
                ),
            )

    def record_lan_progress(
        self, output_id: str, run_id: str, *, stage: Stage, settled: bool
    ) -> None:
        """How ``run_id``'s LAN print goes, as the follow read it (#4644). Only that
        run's row moves: a newer print's is left alone."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_last_prints SET print_stage = %s, print_settled = %s"
                " WHERE output_id = %s AND print_route = 'lan' AND print_run_id = %s"
                " AND (print_stage IS DISTINCT FROM %s OR print_settled <> %s)",
                (stage, settled, output_id, run_id, stage, settled),
            )

    def for_outputs(self, output_ids: Sequence[str]) -> dict[str, LastPrint]:
        if not output_ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT output_id, queue_item_id, print_route, slice_job_id, project_id, plates,"
                " print_run_id, printer::text AS printer, lan_plates, print_stage, print_settled"
                " FROM output_last_prints WHERE output_id = ANY(%s)",
                (list(output_ids),),
            ).fetchall()
        return {row["output_id"]: LastPrint.model_validate(row) for row in rows}

    def delete(self, output_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_last_prints WHERE output_id = %s", (output_id,))
