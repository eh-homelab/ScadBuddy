"""The print watcher on a LAN-only printer (#4699 phase 4): the job's file read over the
printer's FTPS while it prints, kept in ScadBuddy's own library, and recorded through the
API with the same confident match as the Bambuddy path. No Postgres and no printer: the
printer, the library and the recorder are fakes."""

from __future__ import annotations

import asyncio
import dataclasses
import ftplib
import hashlib
import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO, cast

import httpx
import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.bambuddy.models import LibraryFile, LibraryListRow
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_watch import lan as lan_module
from scadbuddy.print_watch.lan import (
    INBOX_FOLDER,
    NO_MATCH,
    NOT_ALLOWED,
    NOT_SERVED,
    LanCapture,
    LanImport,
    LanImportActivities,
    decide,
    plate_entry,
    printed_file,
    printer_paths,
)
from scadbuddy.print_watch.lan_store import LanImportStore, LanPrintRecord, StoreLanRecorder
from scadbuddy.printers.lan import CertificateMismatchError, FileTooLargeError, LanPrinter
from scadbuddy.printers.models import PrinterRow
from scadbuddy.workflows.lan_auto_import import (
    ATTEMPT_TIMEOUT,
    LAN_AUTO_IMPORT_ACTIVITY,
    MAX_ATTEMPTS,
    LanAutoImport,
    LanJob,
    lan_import_id,
    start_lan_import,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
PID = "6f1c3a52-6a43-4d2b-9a3e-0d2f4f0b8c11"
GCODE = "/data/Metadata/plate_2.gcode"
JOB = LanJob(printer_id=PID, subtask_name="demo_plate", started_at=NOW, gcode_file=GCODE)
ROW = PrinterRow(
    id=PID,
    serial="01P00A000000001",
    name="H2C",
    lan_host="10.0.0.9",
    lan_cert_sha256="ab" * 32,
    created_at=NOW,
    updated_at=NOW,
)
WITH_PRINTER = StoredSettings(
    print_auto_import=True,
    print_auto_import_sources=["archive", "printer"],
    library_backend="native",
)


def three_mf(*entries: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        for entry in entries:
            archive.writestr(entry, "G1 X0")
    return out.getvalue()


PRINTED = three_mf("Metadata/plate_2.gcode")
SHA = hashlib.sha256(PRINTED).hexdigest()


# -- which file is the job's ------------------------------------------------------------


def test_the_job_is_looked_for_in_the_cache_first_then_at_the_root() -> None:
    assert printer_paths("demo_plate") == [
        "/cache/demo_plate.gcode.3mf",
        "/cache/demo_plate.3mf",
        "/demo_plate.gcode.3mf",
        "/demo_plate.3mf",
    ]
    assert printer_paths("Benchy.3mf") == ["/cache/Benchy.3mf", "/Benchy.3mf"]


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "a/b", "a\\b", "x\r\nDELE /cache/y.3mf", "x\x00", "..", " . ", "x" * 201],
)
def test_a_name_that_cannot_name_one_file_reads_nothing(name: str) -> None:
    """The name is the printer's text and goes into an FTP command: a path separator or
    a line break would read, or run, something else."""
    assert printer_paths(name) == []


def test_the_printed_file_holds_the_plate_gcode_the_printer_named() -> None:
    assert plate_entry(GCODE) == "Metadata/plate_2.gcode"
    assert plate_entry("/sdcard/x.gcode") is None
    assert printed_file(PRINTED, GCODE)
    # The same name, another job's file: another plate.
    assert not printed_file(three_mf("Metadata/plate_1.gcode"), GCODE)
    assert not printed_file(b"not a zip", GCODE)
    # The printer named no plate: any 3MF of that name.
    assert printed_file(three_mf(), None)


# -- the confident match ----------------------------------------------------------------

KEPT = LanCapture(
    job=JOB,
    source="printer",
    library_file_id=41,
    printer_path="/cache/demo_plate.gcode.3mf",
    filename="demo_plate.gcode.3mf",
    size=len(PRINTED),
    sha256=SHA,
)


def test_an_earlier_import_of_the_same_file_links_its_model() -> None:
    row = decide(KEPT, slugs=["demo-plate", "other"], earlier=["other"])
    assert (row.state, row.slug, row.matched_by) == ("linked", "other", "earlier_import")
    assert row.library_file_id == 41 and row.printer_id == PID and row.reason is None


def test_the_jobs_name_word_for_word_links_its_model() -> None:
    row = decide(KEPT, slugs=["demo-plate", "demo"], earlier=[])
    assert (row.state, row.slug, row.matched_by) == ("linked", "demo-plate", "job_name")


def test_no_confident_match_goes_to_the_inbox_with_suggestions() -> None:
    row = decide(KEPT, slugs=["demo", "plate-stand"], earlier=["a", "b"])
    assert (row.state, row.slug, row.reason) == ("inbox", None, NO_MATCH)
    assert row.suggested == ["demo"]


def test_a_match_without_its_file_goes_to_the_inbox_first_in_suggested() -> None:
    capture = LanCapture(job=JOB, reason="no source had the 3MF (printer: down)")
    row = decide(capture, slugs=["demo-plate", "demo"], earlier=[])
    assert (row.state, row.slug, row.source) == ("inbox", None, None)
    assert row.reason == "no source had the 3MF (printer: down)"
    assert row.suggested == ["demo-plate", "demo"]


# -- reading and keeping the file -------------------------------------------------------


class Printers:
    def __init__(self, row: PrinterRow | None = ROW) -> None:
        self.row = row

    def get(self, printer_id: str) -> PrinterRow | None:
        return self.row

    def open_access_code(self, printer_id: str) -> str:
        return "12345678"


class Lan:
    """The printer's FTPS: ``files`` by path; a path in ``fails`` raises its error."""

    def __init__(self, files: dict[str, bytes], fails: dict[str, BaseException] | None = None):
        self.files = files
        self.fails = fails or {}
        self.read: list[str] = []

    async def download(self, remote_name: str, *, max_bytes: int | None = None) -> bytes:
        self.read.append(remote_name)
        if remote_name in self.fails:
            raise self.fails[remote_name]
        if remote_name not in self.files:
            raise ftplib.error_perm("550 No such file")
        data = self.files[remote_name]
        if max_bytes is not None and len(data) > max_bytes:
            raise FileTooLargeError(max_bytes)
        return data


@dataclasses.dataclass
class Folder:
    id: int


class Library:
    """ScadBuddy's own library: one folder, its files in memory."""

    def __init__(self, fails: BaseException | None = None) -> None:
        self.files: dict[int, tuple[str, bytes]] = {}
        self.uploads: list[tuple[str, int | None, str | None]] = []
        self.listed: list[str | None] = []
        self.fails = fails

    async def template_folder(self, name: str) -> Folder:
        assert name == INBOX_FOLDER
        if self.fails is not None:
            raise self.fails
        return Folder(id=7)

    async def library_listing(
        self, *, folder_id: int | None, filename: str | None = None
    ) -> list[LibraryListRow]:
        self.listed.append(filename)
        return [
            LibraryListRow(id=i, filename=name, file_type="3mf", file_size=len(data))
            for i, (name, data) in self.files.items()
            if filename is None or name == filename
        ]

    async def library_file(self, file_id: int) -> LibraryFile:
        name, data = self.files[file_id]
        return LibraryFile(id=file_id, filename=name, file_hash=hashlib.sha256(data).hexdigest())

    async def upload(
        self, filename: str, data: BinaryIO, folder_id: int | None, *, print_subject: str | None
    ) -> Folder:
        file_id = 40 + len(self.files) + 1
        self.files[file_id] = (filename, data.read())
        self.uploads.append((filename, folder_id, print_subject))
        return Folder(id=file_id)


class Recorder:
    def __init__(self) -> None:
        self.captures: list[LanCapture] = []

    async def record(self, capture: LanCapture) -> LanImport:
        self.captures.append(capture)
        return decide(capture, slugs=["demo-plate"], earlier=[])


def activities(
    lan: Lan,
    *,
    settings: StoredSettings = WITH_PRINTER,
    library: Library | None = None,
    printers: Printers | None = None,
    recorder: Recorder | None = None,
) -> LanImportActivities:
    async def own_library() -> Library | None:
        return library

    return LanImportActivities(
        printers or Printers(),
        lambda: settings,
        own_library,
        recorder or Recorder(),
        connect=lambda *args: cast(LanPrinter, lan),
    )


async def test_the_jobs_file_is_read_from_the_cache_and_kept_in_the_inbox_folder() -> None:
    lan = Lan({"/cache/demo_plate.3mf": PRINTED})
    library = Library()
    capture = await activities(lan, library=library).capture(JOB, last=False)

    assert lan.read == ["/cache/demo_plate.gcode.3mf", "/cache/demo_plate.3mf"]
    assert library.uploads == [("demo_plate.3mf", 7, None)]
    assert capture == LanCapture(
        job=JOB,
        source="printer",
        library_file_id=41,
        printer_path="/cache/demo_plate.3mf",
        filename="demo_plate.3mf",
        size=len(PRINTED),
        sha256=SHA,
    )


async def test_a_copy_an_earlier_attempt_kept_is_used_again() -> None:
    library = Library()
    library.files[12] = ("demo_plate.gcode.3mf", PRINTED)
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    capture = await activities(lan, library=library).capture(JOB, last=False)
    assert capture.library_file_id == 12 and library.uploads == []


async def test_the_inbox_folder_is_listed_by_the_files_name_only() -> None:
    """#6045: the keep lists only the files of the job's name, so its cost does not grow
    with the folder."""
    library = Library()
    library.files[12] = ("another_job.3mf", b"other")
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    capture = await activities(lan, library=library).capture(JOB, last=False)
    assert library.listed == ["demo_plate.gcode.3mf"]
    assert capture.library_file_id == 42


async def test_without_the_printer_source_nothing_reaches_the_printer() -> None:
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    capture = await activities(lan, settings=StoredSettings(print_auto_import=True)).capture(
        JOB, last=False
    )
    assert (capture.source, capture.reason, lan.read) == (None, NOT_ALLOWED, [])


async def test_a_file_of_the_jobs_name_from_another_job_is_not_kept() -> None:
    other = three_mf("Metadata/plate_1.gcode")
    lan = Lan({"/cache/demo_plate.gcode.3mf": other})
    library = Library()
    capture = await activities(lan, library=library).capture(JOB, last=False)
    assert capture.source is None and library.uploads == []
    assert capture.reason is not None and "is not the printed file" in capture.reason
    assert "/demo_plate.3mf is not on the printer" in capture.reason


async def test_a_printer_that_does_not_answer_is_tried_again_until_the_last_attempt() -> None:
    lan = Lan({}, fails={"/cache/demo_plate.gcode.3mf": TimeoutError()})
    with pytest.raises(Exception, match="could not be read"):
        await activities(lan, library=Library()).capture(JOB, last=False)

    capture = await activities(lan, library=Library()).capture(JOB, last=True)
    assert capture.source is None
    assert capture.reason == (
        "no source had the 3MF (printer: /cache/demo_plate.gcode.3mf could not be read:"
        " TimeoutError)"
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ftplib.error_perm("530 Login incorrect"), "it refused the access code"),
        (CertificateMismatchError(), "its certificate is not the one pinned"),
    ],
)
async def test_a_refusal_that_would_only_come_back_is_recorded_at_once(
    error: BaseException, reason: str
) -> None:
    lan = Lan({}, fails={"/cache/demo_plate.gcode.3mf": error})
    capture = await activities(lan, library=Library()).capture(JOB, last=False)
    assert capture.reason is not None and reason in capture.reason
    assert lan.read == ["/cache/demo_plate.gcode.3mf"]


async def test_on_bambuddys_library_the_printer_is_not_read() -> None:
    """The print worker's library client cannot tell which library serves: the stored
    setting is read before the printer, so no file is read only to be refused."""
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    library = Library()
    bambuddy = WITH_PRINTER.model_copy(update={"library_backend": "bambuddy"})
    capture = await activities(lan, settings=bambuddy, library=library).capture(JOB, last=False)
    assert (capture.source, capture.reason, lan.read, library.uploads) == (
        None,
        NOT_SERVED,
        [],
        [],
    )


async def test_a_library_that_does_not_serve_yet_reads_no_printer() -> None:
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    capture = await activities(lan, library=None).capture(JOB, last=False)
    assert (capture.source, capture.reason, lan.read) == (None, NOT_SERVED, [])


async def test_the_last_attempt_records_whatever_the_keep_raised() -> None:
    """An error no rule names (a bug, a malformed answer) is retried, and on the last
    attempt is the record's reason: the print is never left unrecorded."""
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    act = activities(lan, library=Library(fails=RuntimeError("bad answer")))
    with pytest.raises(RuntimeError):
        await act.capture(JOB, last=False)
    capture = await act.capture(JOB, last=True)
    assert capture.source is None
    assert capture.reason == "the printer's file could not be read or kept (RuntimeError)"


class Hangs(Lan):
    """A printer whose read never ends."""

    async def download(self, remote_name: str, *, max_bytes: int | None = None) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError


async def test_a_read_past_its_bound_is_recorded_on_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read and keep end before the attempt's timeout, so the last attempt still has
    time to record the print."""
    monkeypatch.setattr(lan_module, "CAPTURE_TIMEOUT", timedelta(seconds=0.05))
    assert lan_module.CAPTURE_TIMEOUT < ATTEMPT_TIMEOUT
    act = activities(Hangs({}), library=Library())
    with pytest.raises(TimeoutError):
        await act.capture(JOB, last=False)
    capture = await act.capture(JOB, last=True)
    assert capture.reason == "the printer's file could not be read or kept (TimeoutError)"


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        (ApiError(503, "the API is restarting"), True),
        (httpx.ConnectError("refused"), True),
        (ApiError(413, "too large"), False),
    ],
)
async def test_a_library_that_fails_now_is_tried_again(error: Exception, retried: bool) -> None:
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    act = activities(lan, library=Library(fails=error))
    if retried:
        with pytest.raises(Exception, match="the library"):
            await act.capture(JOB, last=False)
    else:
        capture = await act.capture(JOB, last=False)
        assert capture.reason == "the printer's file could not be kept: too large"


async def test_a_deleted_or_unlinked_printer_is_not_dialled() -> None:
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    capture = await activities(lan, printers=Printers(None)).capture(JOB, last=False)
    assert capture.source is None and lan.read == []


async def test_the_activity_records_through_the_recorder_and_answers_the_state() -> None:
    lan = Lan({"/cache/demo_plate.gcode.3mf": PRINTED})
    recorder = Recorder()
    act = activities(lan, library=Library(), recorder=recorder)
    assert await ActivityEnvironment().run(act.lan_auto_import, JOB) == "linked"
    assert [c.source for c in recorder.captures] == ["printer"]


async def test_the_last_attempt_records_the_print_whatever_the_printer_says() -> None:
    lan = Lan({}, fails={"/cache/demo_plate.gcode.3mf": ConnectionResetError()})
    recorder = Recorder()
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=MAX_ATTEMPTS)
    act = activities(lan, library=Library(), recorder=recorder)
    assert await env.run(act.lan_auto_import, JOB) == "inbox"
    assert recorder.captures[0].reason is not None


def test_a_job_has_one_run_whatever_its_name_holds() -> None:
    again = JOB.model_copy()
    assert lan_import_id(JOB) == lan_import_id(again)
    assert lan_import_id(JOB) != lan_import_id(JOB.model_copy(update={"subtask_name": "x"}))
    assert lan_import_id(JOB).startswith(f"print-watch-lan-{PID}-")
    assert "demo_plate" not in lan_import_id(JOB)


# -- the API's recorder -----------------------------------------------------------------


class Store:
    """`LanImportStore`'s reads and write, in memory."""

    def __init__(self, known: LanImport | None = None, earlier: set[str] | None = None):
        self.known = known
        self.earlier = earlier or set()
        self.written: list[LanImport] = []

    async def find(self, printer_id: str, subtask_name: str, started_at: Any) -> LanImport | None:
        return self.known

    async def slugs_by_hash(self, sha256: str) -> set[str]:
        return self.earlier

    async def record(self, row: LanImport) -> LanImport:
        self.written.append(row)
        return row.model_copy(update={"id": 1})


async def test_the_recorder_matches_only_standing_models() -> None:
    """An earlier import's model that was deleted since names nothing: the job's name
    links instead."""
    store = Store(earlier={"gone", "demo-plate"})
    recorder = StoreLanRecorder(
        cast(LanImportStore, store), lambda: ["demo-plate"], lambda slug: slug != "gone"
    )
    row = await recorder.record(KEPT)
    assert (row.id, row.slug, row.matched_by) == (1, "demo-plate", "earlier_import")


async def test_a_job_recorded_before_is_answered_as_it_is() -> None:
    known = decide(KEPT, slugs=[], earlier=[]).model_copy(update={"id": 9})
    store = Store(known=known)
    recorder = StoreLanRecorder(cast(LanImportStore, store), list, lambda slug: True)
    assert await recorder.record(KEPT) == known
    assert store.written == []


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.mark.requires_temporal
async def test_a_job_nudged_twice_is_recorded_by_one_run(client: Client) -> None:
    """A second nudge of a job (a reconnect, a replica) joins its run while it runs, and
    starts nothing once it ended: the job has one record."""
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    seen: list[LanJob] = []

    @activity.defn(name=LAN_AUTO_IMPORT_ACTIVITY)
    async def record(job: LanJob) -> str:
        seen.append(job)
        return "inbox"

    try:
        first = await start_lan_import(client, queue, JOB)
        second = await start_lan_import(client, queue, JOB)
        assert first is not None and second is not None
        assert first.result_run_id == second.result_run_id
        async with Worker(client, task_queue=queue, workflows=[LanAutoImport], activities=[record]):
            assert await first.result() == "inbox"
        assert await start_lan_import(client, queue, JOB) is None
        assert seen == [JOB]
    finally:
        await terminate_open_workflows(client, queue)


def test_a_listed_record_always_names_its_id() -> None:
    """#6048: the routes answer stored rows, so the spec says ``id`` is required, and the
    browser never needs a sentinel id for a record."""
    schema = LanPrintRecord.model_json_schema(mode="serialization")
    assert "id" in schema["required"]
    assert schema["properties"]["id"] == {"title": "Id", "type": "integer"}
