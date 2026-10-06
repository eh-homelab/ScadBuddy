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

A Bambuddy library file printed from ScadBuddy has no output (#976). Its run records
each queue item in ``library_bambuddy_prints`` as it queues it, and the archive is
filled in once the item names one (`linking.link_library_prints`). Both tables feed
the prints API; an archive both name is the output's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
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


class LinkedPrint(PrintLink):
    """A link with what it is a print of: one print of the prints API (#308). Either
    an output (``output_id``) or a Bambuddy library file (``library_file_id``, #976)."""

    output_id: str | None = None
    library_file_id: int | None = None
    #: A library file's name as its queue item reported it; None for an output's.
    name: str | None = None


class PendingLibraryPrint(BaseModel):
    """A library file's queue item whose archive is not known yet (#976)."""

    queue_item_id: int
    library_file_id: int


#: Which of an archive's links owns it: an output's before a library file's, then the
#: first seen, the lower output id on a tie. Every lookup of an owner orders by this,
#: so the proxy's gate, the list and the detail can never name different owners
#: (#609 review).
_OWNER_ORDER = "library_file_id NULLS FIRST, first_seen, output_id, queue_item_id"

#: Every link of both kinds, in one shape.
_LINKS = (
    "(SELECT output_id, NULL::bigint AS library_file_id, NULL::text AS name, archive_id,"
    " matched_by, queue_item_id, plate_id, printer_id, first_seen"
    " FROM output_bambuddy_prints"
    " UNION ALL"
    " SELECT NULL, library_file_id, name, archive_id, 'queue_item', queue_item_id, plate_id,"
    " printer_id, first_seen"
    " FROM library_bambuddy_prints WHERE archive_id IS NOT NULL) AS links"
)

#: One row per archive: its owner, by `_OWNER_ORDER` after ``archive_id``.
_LINKED = f"SELECT DISTINCT ON (archive_id) * FROM {_LINKS}"


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
        linked = await self.linked(archive_id)
        return linked.output_id if linked is not None else None

    async def linked(self, archive_id: int) -> LinkedPrint | None:
        """``archive_id``'s link, with the output or library file that printed it, or
        ``None`` if ScadBuddy printed neither."""
        return await asyncio.to_thread(self._linked, archive_id)

    async def page(
        self,
        *,
        limit: int,
        before: int | None = None,
        output_ids: Sequence[str] | None = None,
    ) -> list[LinkedPrint]:
        """Up to ``limit`` linked archives below ``before``, newest archive first, each
        once; only those of ``output_ids`` when it is given, which leaves out every
        library file's. Bambuddy numbers archives as it creates them, so a higher id is
        a later print."""
        return await asyncio.to_thread(self._page, limit, before, output_ids)

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

    async def record_library(
        self,
        library_file_id: int,
        queue_item_id: int,
        *,
        plate_id: int | None,
        printer_id: int | None,
    ) -> None:
        """Record a queue item a library-file run created (#976); its archive is linked
        later, by `link_library`. Recording one twice changes nothing."""
        await asyncio.to_thread(
            self._record_library, library_file_id, queue_item_id, plate_id, printer_id
        )

    async def pending_library(self, limit: int) -> list[PendingLibraryPrint]:
        """The library files' queue items whose archive is not known yet and that are
        not gone, newest first, at most ``limit``."""
        return await asyncio.to_thread(self._pending_library, limit)

    async def link_library(self, queue_item_id: int, archive_id: int, name: str | None) -> None:
        """The archive a library file's queue item reported."""
        await asyncio.to_thread(self._link_library, queue_item_id, archive_id, name)

    async def library_gone(self, queue_item_id: int) -> None:
        """Bambuddy dropped or settled the item before it named an archive: stop
        reading it."""
        await asyncio.to_thread(self._library_gone, queue_item_id)

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

    def _linked(self, archive_id: int) -> LinkedPrint | None:
        # Nothing here makes an archive unique to one output: two outputs whose sliced
        # files hash the same can both match it by hash, and the earliest link then owns
        # it for the proxy. `OutputStore.create` stamps the output id into each source
        # 3MF (`render/provenance.py`), so the same parameters rendered twice differ at
        # the source; whether the sliced file keeps that stamp is the #306 spike's to
        # confirm. If it does not, one output's prints can show under another's id.
        with self._require().connection() as conn:
            row = conn.execute(
                f"{_LINKED} WHERE archive_id = %s ORDER BY archive_id, {_OWNER_ORDER}",
                (archive_id,),
            ).fetchone()
        return LinkedPrint.model_validate(dict(row)) if row is not None else None

    def _page(
        self, limit: int, before: int | None, output_ids: Sequence[str] | None
    ) -> list[LinkedPrint]:
        with self._require().connection() as conn:
            rows = conn.execute(
                # Each archive's owner is chosen over all its rows first, as `_linked`
                # does, and only then kept or dropped by output: filtering first would
                # hand an archive to whichever filtered output saw it (#609 review).
                f"SELECT * FROM ({_LINKED}"
                " WHERE (%(before)s::bigint IS NULL OR archive_id < %(before)s)"
                f" ORDER BY archive_id DESC, {_OWNER_ORDER}) AS owners"
                " WHERE (%(outputs)s::text[] IS NULL OR output_id = ANY(%(outputs)s))"
                " ORDER BY archive_id DESC LIMIT %(limit)s",
                {
                    "before": before,
                    "outputs": list(output_ids) if output_ids is not None else None,
                    "limit": limit,
                },
            ).fetchall()
        return [LinkedPrint.model_validate(dict(row)) for row in rows]

    def _delete_outputs(self, ids: list[str]) -> None:
        with self._require().connection() as conn:
            conn.execute("DELETE FROM output_bambuddy_prints WHERE output_id = ANY(%s)", (ids,))

    def _record_library(
        self,
        library_file_id: int,
        queue_item_id: int,
        plate_id: int | None,
        printer_id: int | None,
    ) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "INSERT INTO library_bambuddy_prints"
                " (queue_item_id, library_file_id, plate_id, printer_id)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (queue_item_id) DO NOTHING",
                (queue_item_id, library_file_id, plate_id, printer_id),
            )

    def _pending_library(self, limit: int) -> list[PendingLibraryPrint]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT queue_item_id, library_file_id FROM library_bambuddy_prints"
                " WHERE archive_id IS NULL AND NOT gone"
                " ORDER BY first_seen DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [PendingLibraryPrint.model_validate(dict(row)) for row in rows]

    def _link_library(self, queue_item_id: int, archive_id: int, name: str | None) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "UPDATE library_bambuddy_prints SET archive_id = %s, name = %s"
                " WHERE queue_item_id = %s AND archive_id IS NULL",
                (archive_id, name, queue_item_id),
            )

    def _library_gone(self, queue_item_id: int) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "UPDATE library_bambuddy_prints SET gone = true"
                " WHERE queue_item_id = %s AND archive_id IS NULL",
                (queue_item_id,),
            )
