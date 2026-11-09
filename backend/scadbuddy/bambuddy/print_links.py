"""Which Bambuddy archives a print produced (#306, epic #305), and what each run
queued (#1750).

An archive is Bambuddy's record of one print: its outcome, photos and timelapse. It is
created when the print is dispatched, and the queue item it came from gets its id
(``queue_item.archive_id``; verified live on 1.2.5.6, see the print-history plan §1).
ScadBuddy stores only the link, never the archive: Bambuddy stays the source of truth.

Each row of ``print_sends`` and ``print_links`` is keyed by the print's subject
(`subject.PrintSubject`): an output, or a Bambuddy library file (#976). The two kinds
share both tables (#1750).

- ``print_sends``: each queue item a run queued, written by its source's ``record()``
  as each plate is queued (`print_source.record_sends`).
- ``print_links``: each archive a subject's print produced. Two ways a link is found
  (``matched_by``): ``queue_item``, the queue item ScadBuddy created reports
  ``archive_id``; ``content_hash``, once the item is gone, an archive whose
  ``content_hash`` equals a sliced file of the subject's
  (`linking.link_subject_by_hash`, a library file's since #1755), or of a LAN start,
  which has no queue item (`linking.link_lan_starts`, #5724). A library file's
  sends are linked once their item names an archive (`linking.link_library_prints`).
  An archive two subjects name is the output's.

``print_sends`` and ``print_links`` are created by ``*_print_subjects.sql``, from
``output_bambuddy_prints`` and ``library_bambuddy_prints``, which
``*_drop_legacy_print_links.sql`` (#1772) drops.

``lan_print_archives`` holds the LAN start each archive became (#5661). It is keyed by
the archive, has no subject column, and is created by
``20261108T1604Z_lan_print_archives.sql``. The API writes it as it reads a link found by
hash whose file the start uploaded (`print_history._run_by_file`): on the print's first
read, or as the prints list links the print (`print_history._link_lan_prints`, #5724),
which looks only for the starts no row here names. A LAN start has no queue item, so no
send names it.

`PrintLinkStore.jobs` reads these records, with ``print_runs``' LAN starts, to mark the
queue items and printer jobs ScadBuddy queued or started (#3741, `ScadBuddyJob`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.subject import PrintSubject

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
    #: The print run that queued it (#1751); None for a send recorded before.
    run_id: str | None = None
    #: When it was recorded; set by the database.
    first_seen: datetime | None = None


class LanArchiveLink(BaseModel):
    """The LAN start an archive became (#5661): the run that started it, the name it
    started under (`LanStarted.subtask_name`) and its plate."""

    archive_id: int
    run_id: str
    subtask_name: str
    plate_id: int | None = None


#: How a job was found to be ScadBuddy's (#3741): the queue item a run's send recorded,
#: an archive ScadBuddy linked, or the ``subtask_name`` a run's LAN start recorded on that
#: printer.
JobMatchedBy = Literal["queue_item", "archive", "lan_start"]


class ScadBuddyJob(BaseModel):
    """A queue item or printer job that ScadBuddy queued or started (#3741): what the
    Prints page's "Queued from ScadBuddy" badge marks. It is found only by ScadBuddy's
    own records (`PrintLinkStore.jobs`), never by a name or a time, so a job started
    from Bambuddy or on the printer has none."""

    #: The print subject's key: ``output:<id>`` or ``library:<file id>``.
    subject: str
    output_id: str | None = None
    library_file_id: int | None = None
    #: The output's template, when ScadBuddy knows it; ``None`` for a library file.
    slug: str | None = None
    #: The print run that queued or started it, when one is recorded.
    run_id: str | None = None
    #: The archive ScadBuddy linked to it (its print is ``/prints/{archive_id}``).
    archive_id: int | None = None
    matched_by: JobMatchedBy


@dataclass(frozen=True)
class JobMatches:
    """`PrintLinkStore.jobs`'s answer: each key that matched, and the job it is."""

    queue_items: dict[int, ScadBuddyJob] = field(default_factory=dict)
    archives: dict[int, ScadBuddyJob] = field(default_factory=dict)
    #: By ``(registry printer id, subtask_name)``.
    lan_starts: dict[tuple[str, str], ScadBuddyJob] = field(default_factory=dict)


def _output_id_of(column: str) -> str:
    """SQL for the output id a subject column names, or NULL for a library file's."""
    return f"CASE WHEN {column} LIKE 'output:%%' THEN substr({column}, 8) END"


def _job(row: dict[str, Any], subject: PrintSubject, matched_by: JobMatchedBy) -> ScadBuddyJob:
    return ScadBuddyJob(
        subject=subject.key,
        output_id=subject.output_id,
        library_file_id=subject.file_id,
        slug=row["slug"] if subject.output_id is not None else None,
        run_id=row["run_id"],
        archive_id=row["archive_id"],
        matched_by=matched_by,
    )


#: The queue items a send recorded, then those only a link names (a send from before
#: ``print_sends``, #1750), each with its archive when one is linked.
_QUEUE_JOBS = (
    "SELECT s.queue_item_id, s.subject, s.run_id,"
    " COALESCE(o.slug, r.slug) AS slug,"
    " (SELECT l.archive_id FROM print_links l WHERE l.queue_item_id = s.queue_item_id"
    "  AND l.subject = s.subject ORDER BY l.first_seen LIMIT 1) AS archive_id"
    " FROM print_sends s"
    " LEFT JOIN print_runs r ON r.id = s.run_id"
    f" LEFT JOIN output_index o ON o.output_id = {_output_id_of('s.subject')}"
    " WHERE s.queue_item_id = ANY(%(ids)s)"
    " UNION ALL"
    " SELECT * FROM (SELECT DISTINCT ON (l.queue_item_id) l.queue_item_id, l.subject,"
    " NULL::text AS run_id, o.slug, l.archive_id"
    " FROM print_links l"
    f" LEFT JOIN output_index o ON o.output_id = {_output_id_of('l.subject')}"
    " WHERE l.queue_item_id = ANY(%(ids)s) AND NOT EXISTS"
    "  (SELECT 1 FROM print_sends s WHERE s.queue_item_id = l.queue_item_id)"
    " ORDER BY l.queue_item_id, (l.subject LIKE 'library:%%'), l.first_seen, l.subject)"
    " AS legacy"
)

#: Each archive's owner (`_OWNER_ORDER`), with the run that queued its item or made its
#: LAN start.
_ARCHIVE_JOBS = (
    "SELECT DISTINCT ON (l.archive_id) l.archive_id, l.subject,"
    " COALESCE(s.run_id, a.run_id) AS run_id, COALESCE(o.slug, r.slug) AS slug"
    " FROM print_links l"
    " LEFT JOIN print_sends s ON s.queue_item_id = l.queue_item_id"
    " LEFT JOIN lan_print_archives a ON a.archive_id = l.archive_id"
    " LEFT JOIN print_runs r ON r.id = COALESCE(s.run_id, a.run_id)"
    f" LEFT JOIN output_index o ON o.output_id = {_output_id_of('l.subject')}"
    " WHERE l.archive_id = ANY(%(ids)s)"
    " ORDER BY l.archive_id, (l.subject LIKE 'library:%%'), l.first_seen, l.subject,"
    " l.queue_item_id"
)

#: The runs on these printers whose result records a LAN start under one of these
#: names, with the archive that start became when one is recorded.
_LAN_JOBS = (
    "SELECT r.id AS run_id, r.output_id AS run_subject, r.printer::text AS printer,"
    " e.name, COALESCE(o.slug, r.slug) AS slug, a.archive_id"
    " FROM print_runs r CROSS JOIN LATERAL (SELECT x->>'subtask_name' AS name"
    "  FROM jsonb_array_elements(CASE WHEN jsonb_typeof(r.result->'lan') = 'array'"
    "   THEN r.result->'lan' END) AS x) AS e"
    " LEFT JOIN lan_print_archives a ON a.run_id = r.id AND a.subtask_name = e.name"
    " LEFT JOIN output_index o ON o.output_id ="
    "  CASE WHEN r.output_id LIKE 'library:%%' THEN NULL ELSE r.output_id END"
    " WHERE r.printer = ANY(%(printers)s::uuid[]) AND e.name = ANY(%(names)s)"
    " ORDER BY r.created_at"
)


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


#: For sends recorded before their run was (#1073, #1751), how long before the newest
#: one an older one still counts as the same run: such sends are told apart from an
#: earlier run's only by when they were recorded.
LEGACY_RUN_WINDOW = timedelta(hours=24)


def _last_run(sends: list[PrintSend]) -> list[PrintSend]:
    """``sends`` (in the order recorded) of the newest one's run; a newest send with no
    ``run_id`` has those within `LEGACY_RUN_WINDOW` of it that have none either."""
    if not sends:
        return []
    newest = sends[-1]
    if newest.run_id is not None:
        return [send for send in sends if send.run_id == newest.run_id]
    legacy = [send for send in sends if send.run_id is None]
    if newest.first_seen is None:
        return legacy
    return [
        send
        for send in legacy
        if send.first_seen is None or newest.first_seen - send.first_seen <= LEGACY_RUN_WINDOW
    ]


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

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

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
        subjects: Sequence[PrintSubject] | None = None,
    ) -> list[LinkedPrint]:
        """Up to ``limit`` linked archives below ``before``, newest archive first, each
        once; only those ``subjects`` own when it is given (a template's outputs, or
        one library file, #1755). Bambuddy numbers archives as it creates them, so a
        higher id is a later print."""
        return await asyncio.to_thread(self._page, limit, before, subjects)

    async def linked_queue_items(self, subject: PrintSubject) -> set[int]:
        """The queue items whose archive is already recorded, so a poll can skip them."""
        return {
            link.queue_item_id
            for link in await self.for_subject(subject)
            if link.queue_item_id is not None
        }

    async def delete_outputs(self, output_ids: Iterable[str]) -> None:
        """Forget deleted outputs' links and sends, and the attachments of each print no
        link names any more (#3047): ScadBuddy no longer shows that print. Bambuddy's
        archives, their photos and the ``Media/`` copies are not touched."""
        keys = [PrintSubject.output(output_id).key for output_id in output_ids]
        await asyncio.to_thread(self._delete_subjects, keys)

    async def record_sends(self, subject: PrintSubject, sends: Sequence[PrintSend]) -> None:
        """Record the queue items a run queued for ``subject`` (#1750). Recording one
        twice changes nothing: the first record is kept."""
        await asyncio.to_thread(self._record_sends, subject, list(sends))

    async def sends_for(self, subject: PrintSubject) -> list[PrintSend]:
        """The subject's sends, in the order they were recorded."""
        return await asyncio.to_thread(self._sends_for, subject)

    async def send(self, queue_item_id: int) -> PrintSend | None:
        """The send that queued ``queue_item_id`` (#5627), or ``None`` for an item no send
        recorded."""
        return await asyncio.to_thread(self._send, queue_item_id)

    async def last_run(self, subject: PrintSubject) -> list[PrintSend]:
        """The sends of the subject's newest run (#1751), as an output's last print is
        its own run's plates: an earlier run, settled or not, is not among them."""
        return _last_run(await self.sends_for(subject))

    async def run_sends(self, subject: PrintSubject, run_id: str) -> list[PrintSend]:
        """The sends of one run of the subject (#5613), in the order recorded: an older
        run the follow settles on its own."""
        return await asyncio.to_thread(self._sends_for, subject, run_id)

    async def lan_start(self, archive_id: int) -> LanArchiveLink | None:
        """The LAN start ``archive_id`` became (#5661), or ``None`` when none is recorded."""
        return await asyncio.to_thread(self._lan_start, archive_id)

    async def record_lan_start(self, link: LanArchiveLink) -> None:
        """Record the LAN start an archive became. The first record is kept."""
        await asyncio.to_thread(self._record_lan_start, link)

    async def jobs(
        self,
        *,
        queue_items: Sequence[int] = (),
        archives: Sequence[int] = (),
        lan_starts: Sequence[tuple[str, str]] = (),
    ) -> JobMatches:
        """Which of these jobs ScadBuddy queued or started (#3741), in at most three
        queries on one connection, one per kind of key given: ``queue_items`` by the
        sends a run recorded (or a link that names the item), ``archives`` by their
        linked owner, ``lan_starts`` (registry printer id, ``subtask_name``) by a run on
        that printer whose result records that start. A LAN start name two runs share
        matches neither: no match is ever a guess."""
        return await asyncio.to_thread(
            self._jobs, sorted(set(queue_items)), sorted(set(archives)), sorted(set(lan_starts))
        )

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
        with self._pool.connection() as conn:
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
        with self._pool.connection() as conn:
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
        with self._pool.connection() as conn:
            row = conn.execute(
                f"{_LINKED} WHERE archive_id = %s ORDER BY archive_id, {_OWNER_ORDER}",
                (archive_id,),
            ).fetchone()
        return _linked_print(dict(row)) if row is not None else None

    def _page(
        self, limit: int, before: int | None, only: Sequence[PrintSubject] | None
    ) -> list[LinkedPrint]:
        subjects = [subject.key for subject in only] if only is not None else None
        with self._pool.connection() as conn:
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
        with self._pool.connection() as conn, conn.transaction():
            unlinked = conn.execute(
                "DELETE FROM print_links WHERE subject = ANY(%s) RETURNING archive_id", (keys,)
            ).fetchall()
            conn.execute("DELETE FROM print_sends WHERE subject = ANY(%s)", (keys,))
            if unlinked:
                conn.execute(
                    "DELETE FROM print_attachments AS a WHERE a.archive_id = ANY(%s) AND NOT"
                    " EXISTS (SELECT 1 FROM print_links l WHERE l.archive_id = a.archive_id)",
                    (sorted({row["archive_id"] for row in unlinked}),),
                )
                conn.execute(
                    "DELETE FROM lan_print_archives AS a WHERE a.archive_id = ANY(%s) AND NOT"
                    " EXISTS (SELECT 1 FROM print_links l WHERE l.archive_id = a.archive_id)",
                    (sorted({row["archive_id"] for row in unlinked}),),
                )

    def _record_sends(self, subject: PrintSubject, sends: list[PrintSend]) -> None:
        if not sends:
            return
        with self._pool.connection() as conn, conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO print_sends"
                " (queue_item_id, subject, plate_id, printer_id, project_id, slice_job_id,"
                " run_id) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (queue_item_id) DO NOTHING",
                [
                    (
                        send.queue_item_id,
                        subject.key,
                        send.plate_id,
                        send.printer_id,
                        send.project_id,
                        send.slice_job_id,
                        send.run_id,
                    )
                    for send in sends
                ],
            )

    def _sends_for(self, subject: PrintSubject, run_id: str | None = None) -> list[PrintSend]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT queue_item_id, plate_id, printer_id, project_id, slice_job_id,"
                " run_id, first_seen FROM print_sends WHERE subject = %s"
                " AND (%s::text IS NULL OR run_id = %s)"
                " ORDER BY first_seen, queue_item_id",
                (subject.key, run_id, run_id),
            ).fetchall()
        return [PrintSend.model_validate(dict(row)) for row in rows]

    def _send(self, queue_item_id: int) -> PrintSend | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT queue_item_id, plate_id, printer_id, project_id, slice_job_id,"
                " run_id, first_seen FROM print_sends WHERE queue_item_id = %s",
                (queue_item_id,),
            ).fetchone()
        return None if row is None else PrintSend.model_validate(dict(row))

    def _lan_start(self, archive_id: int) -> LanArchiveLink | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT archive_id, run_id, subtask_name, plate_id FROM lan_print_archives"
                " WHERE archive_id = %s",
                (archive_id,),
            ).fetchone()
        return None if row is None else LanArchiveLink.model_validate(dict(row))

    def _record_lan_start(self, link: LanArchiveLink) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO lan_print_archives (archive_id, run_id, subtask_name, plate_id)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (archive_id) DO NOTHING",
                (link.archive_id, link.run_id, link.subtask_name, link.plate_id),
            )

    def _jobs(
        self, queue_items: list[int], archives: list[int], lan_starts: list[tuple[str, str]]
    ) -> JobMatches:
        matches = JobMatches()
        if not (queue_items or archives or lan_starts):
            return matches
        with self._pool.connection() as conn:
            if queue_items:
                for row in conn.execute(_QUEUE_JOBS, {"ids": queue_items}).fetchall():
                    # A send first: the legacy branch reads only items no send names.
                    matches.queue_items.setdefault(
                        row["queue_item_id"],
                        _job(dict(row), PrintSubject.parse(row["subject"]), "queue_item"),
                    )
            if archives:
                for row in conn.execute(_ARCHIVE_JOBS, {"ids": archives}).fetchall():
                    matches.archives[row["archive_id"]] = _job(
                        dict(row), PrintSubject.parse(row["subject"]), "archive"
                    )
            if lan_starts:
                wanted = set(lan_starts)
                found: dict[tuple[str, str], list[ScadBuddyJob]] = {}
                rows = conn.execute(
                    _LAN_JOBS,
                    {
                        "printers": sorted({printer for printer, _ in wanted}),
                        "names": sorted({name for _, name in wanted}),
                    },
                ).fetchall()
                for row in rows:
                    key = (row["printer"], row["name"])
                    if key not in wanted:
                        continue
                    subject = PrintSubject.from_run_subject(row["run_subject"])
                    found.setdefault(key, []).append(_job(dict(row), subject, "lan_start"))
                for key, jobs in found.items():
                    if len({job.run_id for job in jobs}) == 1:
                        matches.lan_starts[key] = jobs[0]
        return matches

    def _pending_library(self, limit: int, max_age: timedelta) -> list[PendingLibraryPrint]:
        with self._pool.connection() as conn:
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
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO print_links"
                " (subject, archive_id, matched_by, queue_item_id, plate_id, printer_id, name)"
                " SELECT subject, %s, 'queue_item', queue_item_id, plate_id, printer_id, %s"
                f" FROM print_sends WHERE queue_item_id = %s AND {_UNLINKED}"
                " ON CONFLICT (subject, archive_id) DO NOTHING",
                (archive_id, name, queue_item_id),
            )

    def _library_gone(self, queue_item_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE print_sends SET gone = true WHERE queue_item_id = %s AND {_UNLINKED}",
                (queue_item_id,),
            )
