"""Which Bambuddy archives a print produced (#306, epic #305), and what each run
queued (#1750).

An archive is Bambuddy's record of one print: its outcome, photos and timelapse. It is
created when the print is dispatched, and the queue item it came from gets its id
(``queue_item.archive_id``; verified live on 1.2.5.6, see the print-history plan §1).
ScadBuddy stores only the link, never the archive: Bambuddy stays the source of truth.

Every row is keyed by the print's subject (`subject.PrintSubject`): an output, or a
Bambuddy library file (#976). The two kinds share both tables (#1750).

- ``print_sends``: each queue item a run queued, written by its source's ``record()``
  as each plate is queued (`print_source.record_sends`).
- ``print_links``: each archive a subject's print produced. Two ways a link is found
  (``matched_by``): ``queue_item``, the queue item ScadBuddy created reports
  ``archive_id``; ``content_hash``, once the item is gone, an archive whose
  ``content_hash`` equals a sliced file of the output's (`linking.link_by_hash`). A
  library file's sends are linked once their item names an archive
  (`linking.link_library_prints`). An archive two subjects name is the output's.

Both are created by ``*_print_subjects.sql``, from ``output_bambuddy_prints`` and
``library_bambuddy_prints``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.subject import PrintSubject
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
    """A link with what it is a print of: one print of the prints API (#308). Its
    ``subject`` key, and that subject as the API names it: an output (``output_id``) or
    a Bambuddy library file (``library_file_id``, #976)."""

    subject: str
    output_id: str | None = None
    library_file_id: int | None = None
    #: A library file's name as its queue item reported it; None for an output's.
    name: str | None = None


class PrintSend(BaseModel):
    """One queue item a run queued (#1750), whatever it printed."""

    queue_item_id: int
    plate_id: int | None = None
    printer_id: int | None = None
    project_id: int | None = None
    slice_job_id: int | None = None
    #: When it was recorded; set by the database.
    first_seen: datetime | None = None


class PendingLibraryPrint(BaseModel):
    """A library file's queue item whose archive is not known yet (#976)."""

    queue_item_id: int
    library_file_id: int


#: A subject that is a library file's; every other is an output's.
_LIBRARY = "subject LIKE 'library:%%'"

#: Which of an archive's links owns it: an output's before a library file's, then the
#: first seen, the lower subject on a tie. Every lookup of an owner orders by this, so
#: the proxy's gate, the list and the detail can never name different owners (#609
#: review). The ``print_links_owner`` index is in this order.
_OWNER_ORDER = f"({_LIBRARY}), first_seen, subject, queue_item_id"

#: One row per archive: its owner, by `_OWNER_ORDER` after ``archive_id``.
_LINKED = "SELECT DISTINCT ON (archive_id) * FROM print_links"

#: A library file's send whose archive is not linked yet.
_UNLINKED = (
    f"{_LIBRARY} AND NOT gone AND NOT EXISTS"
    " (SELECT 1 FROM print_links l WHERE l.queue_item_id = print_sends.queue_item_id"
    "  AND l.subject = print_sends.subject)"
)


def _linked_print(row: dict[str, Any]) -> LinkedPrint:
    subject = PrintSubject.parse(row["subject"])
    return LinkedPrint.model_validate(
        {**row, "output_id": subject.output_id, "library_file_id": subject.file_id}
    )


class PrintLinkStore:
    """``print_links`` and ``print_sends``, on the process's pool (the render queue's).

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

    async def record(self, subject: PrintSubject, link: PrintLink) -> None:
        """Record a link. A second sighting of the same archive changes nothing: the
        first queue item and time it was seen with are the ones kept."""
        await asyncio.to_thread(self._record, subject, link)

    async def for_subject(self, subject: PrintSubject) -> list[PrintLink]:
        """The subject's links, in the order they were first seen."""
        return await asyncio.to_thread(self._for_subject, subject)

    async def output_for(self, archive_id: int) -> str | None:
        """The output that printed ``archive_id``, or ``None`` if none of ScadBuddy's did."""
        linked = await self.linked(archive_id)
        return linked.output_id if linked is not None else None

    async def linked(self, archive_id: int) -> LinkedPrint | None:
        """``archive_id``'s link, with the subject that printed it, or ``None`` if
        ScadBuddy printed neither."""
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

    async def linked_queue_items(self, subject: PrintSubject) -> set[int]:
        """The queue items whose archive is already recorded, so a poll can skip them."""
        return {
            link.queue_item_id
            for link in await self.for_subject(subject)
            if link.queue_item_id is not None
        }

    async def delete_outputs(self, output_ids: Iterable[str]) -> None:
        """Forget deleted outputs' links and sends. Bambuddy's archives are not touched."""
        keys = [PrintSubject.output(output_id).key for output_id in output_ids]
        await asyncio.to_thread(self._delete_subjects, keys)

    async def record_sends(self, subject: PrintSubject, sends: Sequence[PrintSend]) -> None:
        """Record the queue items a run queued for ``subject`` (#1750). Recording one
        twice changes nothing: the first record is kept."""
        await asyncio.to_thread(self._record_sends, subject, list(sends))

    async def sends_for(self, subject: PrintSubject) -> list[PrintSend]:
        """The subject's sends, in the order they were recorded."""
        return await asyncio.to_thread(self._sends_for, subject)

    async def pending_library(self, limit: int, *, max_age: timedelta) -> list[PendingLibraryPrint]:
        """The library files' queue items whose archive is not known yet and that are
        not gone, newest first, at most ``limit``. An item recorded more than
        ``max_age`` ago is marked gone first, so it is not returned again (#1703)."""
        return await asyncio.to_thread(self._pending_library, limit, max_age)

    async def link_library(self, queue_item_id: int, archive_id: int, name: str | None) -> None:
        """The archive a library file's queue item reported. The first one is kept."""
        await asyncio.to_thread(self._link_library, queue_item_id, archive_id, name)

    async def library_gone(self, queue_item_id: int) -> None:
        """Bambuddy dropped or settled the item before it named an archive: stop
        reading it."""
        await asyncio.to_thread(self._library_gone, queue_item_id)

    def _record(self, subject: PrintSubject, link: PrintLink) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "INSERT INTO print_links"
                " (subject, archive_id, queue_item_id, plate_id, printer_id, matched_by)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject, archive_id) DO NOTHING",
                (
                    subject.key,
                    link.archive_id,
                    link.queue_item_id,
                    link.plate_id,
                    link.printer_id,
                    link.matched_by,
                ),
            )

    def _for_subject(self, subject: PrintSubject) -> list[PrintLink]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT archive_id, matched_by, queue_item_id, plate_id, printer_id, first_seen"
                " FROM print_links WHERE subject = %s"
                " ORDER BY first_seen, archive_id",
                (subject.key,),
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
        return _linked_print(dict(row)) if row is not None else None

    def _page(
        self, limit: int, before: int | None, output_ids: Sequence[str] | None
    ) -> list[LinkedPrint]:
        subjects = (
            [PrintSubject.output(output_id).key for output_id in output_ids]
            if output_ids is not None
            else None
        )
        with self._require().connection() as conn:
            rows = conn.execute(
                # Each archive's owner is chosen over all its rows first, as `_linked`
                # does, and only then kept or dropped by subject: filtering first would
                # hand an archive to whichever filtered output saw it (#609 review).
                f"SELECT * FROM ({_LINKED}"
                " WHERE (%(before)s::bigint IS NULL OR archive_id < %(before)s)"
                f" ORDER BY archive_id DESC, {_OWNER_ORDER}) AS owners"
                " WHERE (%(subjects)s::text[] IS NULL OR subject = ANY(%(subjects)s))"
                " ORDER BY archive_id DESC LIMIT %(limit)s",
                {"before": before, "subjects": subjects, "limit": limit},
            ).fetchall()
        return [_linked_print(dict(row)) for row in rows]

    def _delete_subjects(self, keys: list[str]) -> None:
        with self._require().connection() as conn, conn.transaction():
            conn.execute("DELETE FROM print_links WHERE subject = ANY(%s)", (keys,))
            conn.execute("DELETE FROM print_sends WHERE subject = ANY(%s)", (keys,))

    def _record_sends(self, subject: PrintSubject, sends: list[PrintSend]) -> None:
        if not sends:
            return
        with self._require().connection() as conn, conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO print_sends"
                " (queue_item_id, subject, plate_id, printer_id, project_id, slice_job_id)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (queue_item_id) DO NOTHING",
                [
                    (
                        send.queue_item_id,
                        subject.key,
                        send.plate_id,
                        send.printer_id,
                        send.project_id,
                        send.slice_job_id,
                    )
                    for send in sends
                ],
            )

    def _sends_for(self, subject: PrintSubject) -> list[PrintSend]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT queue_item_id, plate_id, printer_id, project_id, slice_job_id,"
                " first_seen FROM print_sends WHERE subject = %s"
                " ORDER BY first_seen, queue_item_id",
                (subject.key,),
            ).fetchall()
        return [PrintSend.model_validate(dict(row)) for row in rows]

    def _pending_library(self, limit: int, max_age: timedelta) -> list[PendingLibraryPrint]:
        with self._require().connection() as conn:
            conn.execute(
                f"UPDATE print_sends SET gone = true WHERE {_UNLINKED} AND first_seen < now() - %s",
                (max_age,),
            )
            rows = conn.execute(
                f"SELECT queue_item_id, subject FROM print_sends WHERE {_UNLINKED}"
                " ORDER BY first_seen DESC LIMIT %s",
                (limit,),
            ).fetchall()
        pending = []
        for row in rows:
            file_id = PrintSubject.parse(row["subject"]).file_id
            assert file_id is not None  # `_UNLINKED` reads library files' sends only
            pending.append(
                PendingLibraryPrint(queue_item_id=row["queue_item_id"], library_file_id=file_id)
            )
        return pending

    def _link_library(self, queue_item_id: int, archive_id: int, name: str | None) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "INSERT INTO print_links"
                " (subject, archive_id, matched_by, queue_item_id, plate_id, printer_id, name)"
                " SELECT subject, %s, 'queue_item', queue_item_id, plate_id, printer_id, %s"
                f" FROM print_sends WHERE queue_item_id = %s AND {_UNLINKED}"
                " ON CONFLICT (subject, archive_id) DO NOTHING",
                (archive_id, name, queue_item_id),
            )

    def _library_gone(self, queue_item_id: int) -> None:
        with self._require().connection() as conn:
            conn.execute(
                f"UPDATE print_sends SET gone = true WHERE queue_item_id = %s AND {_UNLINKED}",
                (queue_item_id,),
            )
