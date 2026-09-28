"""Which Bambuddy archives an output's prints produced (#306, epic #305).

An archive is Bambuddy's record of one print: its outcome, photos and timelapse. It is
created when the print is dispatched, and the queue item it came from gets its id
(``queue_item.archive_id``; verified live on 1.2.5.6, see the print-history plan §1).
ScadBuddy stores only the link, never the archive: Bambuddy stays the source of truth.

Two ways a link is found (``matched_by``):

- ``queue_item``: the queue item ScadBuddy created reports ``archive_id``;
- ``content_hash``: once the item is gone, an archive whose ``content_hash`` equals a
  sliced file of the output's (`linking.link_by_hash`).

The table is ``output_bambuddy_prints`` (migration ``*_output_bambuddy_prints.sql``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.uploads import DatabaseRequiredError

MatchedBy = Literal["queue_item", "content_hash"]


class PrintLink(BaseModel):
    archive_id: int
    matched_by: MatchedBy
    queue_item_id: int | None = None
    plate_id: int | None = None
    printer_id: int | None = None
    #: When ScadBuddy first saw the link; set by the database.
    first_seen: datetime | None = None


class PrintLinkStore:
    """``output_bambuddy_prints``, on the process's pool (the render queue's).

    Every public method is a coroutine that runs its query in a worker thread, like
    `BambuddyUploadStore`: the progress read records links on every poll.
    """

    def __init__(self, pool: ConnectionPool[Connection[DictRow]] | None) -> None:
        self._pool = pool

    @property
    def available(self) -> bool:
        """Whether there is a database to record links in; without one they are skipped."""
        return self._pool is not None

    def _require(self) -> ConnectionPool[Connection[DictRow]]:
        if self._pool is None:
            raise DatabaseRequiredError
        return self._pool

    async def record(self, output_id: str, link: PrintLink) -> None:
        """Record a link. A second sighting of the same archive changes nothing: the
        first queue item and time it was seen with are the ones kept."""
        await asyncio.to_thread(self._record, output_id, link)

    async def for_output(self, output_id: str) -> list[PrintLink]:
        """The output's links, in the order they were first seen."""
        return await asyncio.to_thread(self._for_output, output_id)

    async def output_for(self, archive_id: int) -> str | None:
        """The output that printed ``archive_id``, or ``None`` if none of ScadBuddy's did."""
        return await asyncio.to_thread(self._output_for, archive_id)

    async def linked_queue_items(self, output_id: str) -> set[int]:
        """The queue items whose archive is already recorded, so a poll can skip them."""
        return {
            link.queue_item_id
            for link in await self.for_output(output_id)
            if link.queue_item_id is not None
        }

    async def delete_outputs(self, output_ids: Iterable[str]) -> None:
        """Forget deleted outputs' links. Bambuddy's archives are not touched."""
        await asyncio.to_thread(self._delete_outputs, list(output_ids))

    def _record(self, output_id: str, link: PrintLink) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "INSERT INTO output_bambuddy_prints"
                " (output_id, archive_id, queue_item_id, plate_id, printer_id, matched_by)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (output_id, archive_id) DO NOTHING",
                (
                    output_id,
                    link.archive_id,
                    link.queue_item_id,
                    link.plate_id,
                    link.printer_id,
                    link.matched_by,
                ),
            )

    def _for_output(self, output_id: str) -> list[PrintLink]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT archive_id, matched_by, queue_item_id, plate_id, printer_id, first_seen"
                " FROM output_bambuddy_prints WHERE output_id = %s"
                " ORDER BY first_seen, archive_id",
                (output_id,),
            ).fetchall()
        return [PrintLink.model_validate(dict(row)) for row in rows]

    def _output_for(self, archive_id: int) -> str | None:
        # Nothing here makes an archive unique to one output: two outputs whose sliced
        # files hash the same (the same parameters rendered twice) can both match it by
        # hash. The earliest link then owns it for the proxy.
        with self._require().connection() as conn:
            row = conn.execute(
                "SELECT output_id FROM output_bambuddy_prints WHERE archive_id = %s"
                " ORDER BY first_seen LIMIT 1",
                (archive_id,),
            ).fetchone()
        return str(row["output_id"]) if row is not None else None

    def _delete_outputs(self, ids: list[str]) -> None:
        with self._require().connection() as conn:
            conn.execute("DELETE FROM output_bambuddy_prints WHERE output_id = ANY(%s)", (ids,))
