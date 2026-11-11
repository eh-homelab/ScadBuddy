"""The print watcher on a LAN-only printer (#4699 phase 4, design §2 "LAN path", §3, §4).

A printer with no Bambuddy link has no archive, so the watcher keeps a print record of
its own (``print_lan_imports``): one per job, keyed by the registry printer, the job's
``subtask_name`` and its start. The printer's LAN watch sees the start, its nudge starts
the job's `LanAutoImport` run, and the run's one activity (`LanImportActivities`) does
the rest:

1. It reads the job's 3MF over the printer's own FTPS (`printers/lan.py`
   `LanPrinter.download`), at once, while the job prints: the printer keeps a started
   job's file at ``/cache/<name>.gcode.3mf`` until a later job takes its place
   (`printers/lan.py`, Task 7's measurement), and a file sent to the card sits at its
   root (`print_import.PRINTER_DIRECTORIES`). Only when ``print_auto_import_sources``
   names ``printer``: reading a printer is a person's choice, as on the Bambuddy path.
   The printer gives no hash of the file, so a file is the job's when its name is the
   job's and it holds the plate G-code the printer named (`printed_file`); a file of
   that name without it is another job's.
2. It keeps the file in ScadBuddy's own library, in the top-level folder `INBOX_FOLDER`
   (through the library's existing write: the print worker's `RemoteLibrary`, the API's
   `NativeLibrary`), as the Bambuddy path keeps one in Bambuddy's inbox folder. A copy of
   the same name and hash there already (an earlier attempt that was not recorded) is
   used again. On ``library_backend=bambuddy`` ScadBuddy's own library does not serve:
   the print is recorded with no file and `NOT_SERVED`, and the printer is never read
   (the stored setting is checked before the read, as the print worker's library client
   cannot tell).
3. It records the print through the API (`LanRecorder`: the print worker's CRUD route,
   `crud/print.py` `RemoteLanImports`, or the API's store in-process), which matches a
   model as the Bambuddy path does (`decide`): an earlier import of the same printed file
   names one model, or the job's name has exactly one model's words. Anything else goes
   to the inbox with why.

A read or a keep that fails for a reason that may pass (no answer, a dropped
connection, a 5xx) raises, so Temporal tries the attempt again (`LAN_IMPORT_RETRY`); the
last attempt records the print in the inbox with the reason, whatever the read or the
keep raised, and its read and keep are bounded by `CAPTURE_TIMEOUT`, so the attempt has
time left to record. Only a record the API never takes is lost on the last attempt. A
refusal that would only come back (a wrong access code, a certificate that is not the
pinned one, a file too large) is recorded at once. The access code is opened from the
row inside the attempt, never passed in its input, and goes nowhere but the connection.
"""

from __future__ import annotations

import asyncio
import ftplib
import hashlib
import io
import logging
import re
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import BinaryIO, Literal, Protocol

import httpx
from pydantic import BaseModel, Field
from temporalio import activity

from scadbuddy.bambuddy.models import LibraryFile, LibraryListRow
from scadbuddy.bambuddy.print_import import (
    PRINTER_DIRECTORIES,
    SLICED_SUFFIX,
    exact_slugs,
    suggested_slugs,
)
from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.lan import CertificateMismatchError, FileTooLargeError, LanPrinter
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import KekMissingError, NoAccessCodeError
from scadbuddy.workflows.lan_auto_import import (
    ATTEMPT_TIMEOUT,
    LAN_AUTO_IMPORT_ACTIVITY,
    MAX_ATTEMPTS,
    LanJob,
)

logger = logging.getLogger(__name__)

#: The top-level folder of ScadBuddy's own library a LAN-only printer's file is kept in.
INBOX_FOLDER = "Unsorted imports"
#: A job's name the watcher reads a file by: one path segment, no control character (an
#: FTP command ends at a line break), at most 200 characters.
_JOB_NAME = re.compile(r"[^/\\\x00-\x1f\x7f]{1,200}")
#: The plate G-code a report names, and its entry in a sliced 3MF.
_PLATE_GCODE = re.compile(r"/data/(Metadata/plate_\d+\.gcode)")

NO_MATCH = "no model matched with confidence"
NOT_ALLOWED = "no source had the 3MF (printer: not allowed in Settings)"
NOT_SERVED = (
    "the printer's file is kept in ScadBuddy's own library, which does not serve here"
    " (library_backend is bambuddy)"
)
#: The bound of an attempt's read and keep: the attempt's own, less a minute for the
#: record, so the last attempt can still record a print whose read hung.
CAPTURE_TIMEOUT = ATTEMPT_TIMEOUT - timedelta(minutes=1)

LanState = Literal["linked", "inbox", "dismissed"]
#: How a model was matched: as on the Bambuddy path, or a person's link (``import``).
LanMatchedBy = Literal["earlier_import", "job_name", "import"]


class LanCapture(BaseModel):
    """What the print worker found for a job: the kept file, or why there is none."""

    job: LanJob
    source: Literal["printer"] | None = None
    #: A file of ScadBuddy's own library, in `INBOX_FOLDER`.
    library_file_id: int | None = None
    printer_path: str | None = None
    filename: str | None = None
    size: int | None = None
    sha256: str | None = None
    reason: str | None = None


class LanImport(BaseModel):
    """One job on a LAN-only printer the watcher recorded (``print_lan_imports``)."""

    #: Set by the database.
    id: int | None = None
    #: The registry printer; None once the printer was deleted.
    printer_id: str | None = None
    subtask_name: str
    started_at: datetime
    state: LanState
    slug: str | None = None
    matched_by: LanMatchedBy | None = None
    source: Literal["printer"] | None = None
    library_file_id: int | None = None
    printer_path: str | None = None
    filename: str | None = None
    size: int | None = None
    sha256: str | None = None
    reason: str | None = None
    suggested: list[str] = Field(default_factory=list)
    seen_at: datetime | None = None
    dismissed_at: datetime | None = None


def printer_paths(subtask_name: str) -> list[str]:
    """Where the printer may keep the job's file, the cache first: the job's name with
    `SLICED_SUFFIX`, then with ``.3mf``, in each of `PRINTER_DIRECTORIES`. None for a
    name that cannot name a file (a path separator, a control character, only dots)."""
    if not _JOB_NAME.fullmatch(subtask_name) or not subtask_name.strip(". "):
        return []
    lower = subtask_name.lower()
    names = (
        [subtask_name]
        if lower.endswith(".3mf")
        else [f"{subtask_name}{SLICED_SUFFIX}", f"{subtask_name}.3mf"]
    )
    return [
        f"{directory.rstrip('/')}/{name}" for directory in PRINTER_DIRECTORIES for name in names
    ]


def plate_entry(gcode_file: str | None) -> str | None:
    """The sliced 3MF's entry for the plate G-code a report names
    (``/data/Metadata/plate_2.gcode`` is ``Metadata/plate_2.gcode``), or None."""
    if gcode_file is None:
        return None
    found = _PLATE_GCODE.fullmatch(gcode_file)
    return None if found is None else found.group(1)


def printed_file(data: bytes, gcode_file: str | None) -> bool:
    """Whether ``data`` can be the job's file: a 3MF (a ZIP) that holds the plate G-code
    the printer named, when it named one."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    entry = plate_entry(gcode_file)
    return entry is None or entry in names


def decide(capture: LanCapture, *, slugs: Sequence[str], earlier: Sequence[str]) -> LanImport:
    """The record of a capture, matched as the Bambuddy path matches (design §4):
    ``earlier`` (the standing models earlier imports of the same file were linked to)
    names one model, or the job's name has exactly one slug's words. A match with no
    file goes to the inbox, that model first in ``suggested``."""
    job = capture.job
    slug: str | None = None
    matched_by: LanMatchedBy | None = None
    if len(earlier) == 1:
        slug, matched_by = earlier[0], "earlier_import"
    else:
        named = exact_slugs(slugs, job.subtask_name)
        if len(named) == 1:
            slug, matched_by = named[0], "job_name"
    suggested = [
        *([slug] if slug is not None else []),
        *(s for s in suggested_slugs(slugs, job.subtask_name) if s != slug),
    ]
    row = LanImport(
        printer_id=job.printer_id,
        subtask_name=job.subtask_name,
        started_at=job.started_at,
        state="inbox",
        source=capture.source,
        library_file_id=capture.library_file_id,
        printer_path=capture.printer_path,
        filename=capture.filename,
        size=capture.size,
        sha256=capture.sha256,
        suggested=suggested,
    )
    if slug is not None and capture.source is not None:
        row.state, row.slug, row.matched_by = "linked", slug, matched_by
    elif capture.source is not None:
        row.reason = NO_MATCH
    else:
        row.reason = capture.reason
    return row


class _HasId(Protocol):
    @property
    def id(self) -> int: ...


class InboxLibrary(Protocol):
    """ScadBuddy's own library, as `RemoteLibrary` (the print worker) and `NativeLibrary`
    (the API) both serve it."""

    async def template_folder(self, name: str) -> _HasId: ...

    async def library_listing(
        self, *, folder_id: int | None, every_folder: bool = False
    ) -> list[LibraryListRow]: ...

    async def library_file(self, file_id: int) -> LibraryFile: ...

    async def upload(
        self, filename: str, data: BinaryIO, folder_id: int | None, *, print_subject: str | None
    ) -> _HasId: ...


class LanRecorder(Protocol):
    """Records a capture and answers the record: a job recorded before is answered as it
    is."""

    async def record(self, capture: LanCapture) -> LanImport: ...


class LanPrinters(Protocol):
    """`PrinterStore`'s two reads the activity makes (blocking, run in a thread)."""

    def get(self, printer_id: str) -> PrinterRow | None: ...

    def open_access_code(self, printer_id: str) -> str: ...


Connect = Callable[[str, str, str, str], LanPrinter]


async def keep(library: InboxLibrary, name: str, data: bytes, sha256: str) -> int:
    """The library file of ``data`` in `INBOX_FOLDER`: one of the same name and hash
    there already (an earlier attempt that was not recorded), or a new upload."""
    folder = await library.template_folder(INBOX_FOLDER)
    for row in await library.library_listing(folder_id=folder.id):
        if row.filename != name or row.file_size not in (None, len(data)):
            continue
        if (await library.library_file(row.id)).file_hash == sha256:
            return row.id
    uploaded = await library.upload(name, io.BytesIO(data), folder.id, print_subject=None)
    return uploaded.id


class _Retry(Exception):  # noqa: N818 - a retry signal, not an error
    """A failure that may pass: the attempt raises it for Temporal to try again."""


class LanImportActivities:
    """The ``lan_auto_import`` activity. ``load_settings`` is blocking (run in a thread);
    ``library`` answers ScadBuddy's own library, or None when it does not serve."""

    def __init__(
        self,
        printers: LanPrinters,
        load_settings: Callable[[], StoredSettings],
        library: Callable[[], Awaitable[InboxLibrary | None]],
        recorder: LanRecorder,
        *,
        connect: Connect = LanPrinter,
    ) -> None:
        self.printers = printers
        self.load_settings = load_settings
        self.library = library
        self.recorder = recorder
        self.connect = connect

    @activity.defn(name=LAN_AUTO_IMPORT_ACTIVITY)
    async def lan_auto_import(self, job: LanJob) -> str:
        last = activity.info().attempt >= MAX_ATTEMPTS
        capture = await self.capture(job, last=last)
        row = await self.recorder.record(capture)
        logger.info(
            "recorded a print on a LAN-only printer",
            extra={"printer_id": job.printer_id, "state": row.state, "source": row.source},
        )
        return row.state

    async def capture(self, job: LanJob, *, last: bool) -> LanCapture:
        """The job's file read and kept, or why not, within `CAPTURE_TIMEOUT`. Raises a
        failure, unless ``last``: then whatever failed (a read that may pass, one past the
        bound, any other error) is the capture's reason, so the print is still recorded.
        A read past the bound stops its FTPS thread too (`LanPrinter.download`)."""
        try:
            return await asyncio.wait_for(self._capture(job), CAPTURE_TIMEOUT.total_seconds())
        except _Retry as failure:
            if not last:
                raise
            return LanCapture(job=job, reason=f"no source had the 3MF (printer: {failure})")
        except Exception as error:
            if not last:
                raise
            logger.warning(
                "the last attempt could not read or keep a LAN-only printer's file",
                extra={"printer_id": job.printer_id},
                exc_info=True,
            )
            return LanCapture(
                job=job,
                reason=f"the printer's file could not be read or kept ({type(error).__name__})",
            )

    async def _capture(self, job: LanJob) -> LanCapture:
        settings = await asyncio.to_thread(self.load_settings)
        if "printer" not in settings.auto_import_sources():
            return LanCapture(job=job, reason=NOT_ALLOWED)
        if settings.library_backend != "native":
            return LanCapture(job=job, reason=NOT_SERVED)
        paths = printer_paths(job.subtask_name)
        if not paths:
            return LanCapture(
                job=job, reason="no source had the 3MF (printer: the job's name names no file)"
            )
        row = await asyncio.to_thread(self.printers.get, job.printer_id)
        if row is None or row.lan_host is None or row.lan_cert_sha256 is None:
            return LanCapture(
                job=job,
                reason="no source had the 3MF (printer: it was deleted or unlinked from the LAN)",
            )
        try:
            code = await asyncio.to_thread(self.printers.open_access_code, job.printer_id)
        except (KekMissingError, NoAccessCodeError):
            return LanCapture(
                job=job,
                reason="no source had the 3MF (printer: its access code cannot be opened here)",
            )
        library = await self.library()
        if library is None:
            # The API's own library on `native` before it serves: nothing to keep it in.
            return LanCapture(job=job, reason=NOT_SERVED)
        lan = self.connect(row.lan_host, row.serial, code, row.lan_cert_sha256)
        found = await self._read(lan, job, paths)
        if isinstance(found, str):
            return LanCapture(job=job, reason=f"no source had the 3MF (printer: {found})")
        path, data = found
        name = PurePosixPath(path).name
        sha256 = hashlib.sha256(data).hexdigest()
        try:
            file_id = await keep(library, name, data, sha256)
        except httpx.TransportError as error:
            raise _Retry(f"the library did not answer: {error}") from error
        except ApiError as error:
            if error.status >= 500:
                raise _Retry(f"the library answered {error.status}: {error.detail}") from error
            return LanCapture(
                job=job, reason=f"the printer's file could not be kept: {error.detail}"
            )
        return LanCapture(
            job=job,
            source="printer",
            library_file_id=file_id,
            printer_path=path,
            filename=name,
            size=len(data),
            sha256=sha256,
        )

    async def _read(
        self, lan: LanPrinter, job: LanJob, paths: Sequence[str]
    ) -> tuple[str, bytes] | str:
        """The first of ``paths`` that holds the job's file, and its bytes; else why none
        does. A failure that may pass raises `_Retry`."""
        problems: list[str] = []
        for path in paths:
            try:
                data = await lan.download(path, max_bytes=MAX_DOWNLOAD_BYTES)
            except CertificateMismatchError:
                return "its certificate is not the one pinned when it was linked"
            except ftplib.error_perm as error:
                reply = str(error)
                if reply.startswith("530"):
                    return "it refused the access code"
                problems.append(f"{path} is not on the printer")
                continue
            except FileTooLargeError as error:
                problems.append(f"{path}: {error}")
                continue
            except (OSError, EOFError, ftplib.Error) as error:
                raise _Retry(f"{path} could not be read: {type(error).__name__}") from error
            if printed_file(data, job.gcode_file):
                return path, data
            problems.append(f"{path} is not the printed file (it lacks the plate's G-code)")
        return "; ".join(problems)
