"""#4699 — the print watcher's choices, without Postgres: which source it takes, which
model it links, and what goes to the inbox. The pass against Postgres and a mocked
Bambuddy is `tests/api/test_print_watch.py`."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_import import FoundSource, SourceFile, exact_slugs
from scadbuddy.bambuddy.print_links import ImportBusyError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_watch import sweep
from scadbuddy.print_watch.store import AutoImport
from scadbuddy.print_watch.sweep import NO_MATCH, AutoImporter, _bambuddy_source, _why_none

NOW = datetime.now(UTC)


def test_only_the_exact_words_of_a_slug_name_it() -> None:
    slugs = ["demo", "demo-plate", "builtin:keychain", "plate-demo"]
    assert exact_slugs(slugs, "demo_plate.gcode.3mf") == ["demo-plate", "plate-demo"]
    assert exact_slugs(slugs, "Keychain.3mf") == ["builtin:keychain"]
    assert exact_slugs(slugs, "demo_elan") == []
    assert exact_slugs(slugs, None) == []


def found(kind: Any, **fields: Any) -> FoundSource:
    return FoundSource(kind=kind, available=True, **fields)


def test_a_library_file_with_the_name_only_is_never_taken() -> None:
    by_name = found("library", name="demo.3mf", library_file_id=7, matched_by="name")
    by_hash = found("library", name="demo.3mf", library_file_id=8, matched_by="hash")
    missing = FoundSource(kind="archive", available=False, reason="gone")
    allowed: Any = ("archive", "archive_source", "library")
    assert _bambuddy_source([missing, by_name], allowed) is None
    assert _bambuddy_source([missing, by_name, by_hash], allowed) == by_hash
    why = _why_none([missing, by_name], allowed)
    assert "archive: gone" in why and "has this print's name but not its hash" in why
    assert "printer: not allowed" in why


def test_the_archive_comes_first_and_a_source_not_allowed_is_skipped() -> None:
    archive = found("archive", name="a.gcode.3mf")
    project = found("archive_source", name="a.3mf")
    assert _bambuddy_source([project, archive], ("archive", "archive_source")) == archive
    assert _bambuddy_source([project, archive], ("archive_source",)) == project


class Links:
    def __init__(self, busy: set[int] | None = None) -> None:
        self.busy = busy or set()

    async def linked_among(self, ids: list[int]) -> set[int]:
        return set()

    async def linked(self, archive_id: int) -> None:
        return None

    @asynccontextmanager
    async def import_lock(self, archive_id: int) -> AsyncIterator[None]:
        if archive_id in self.busy:
            raise ImportBusyError(archive_id)
        yield


class Store:
    def __init__(self, earlier: set[str] | None = None, *, owned: bool = False) -> None:
        self.rows: list[AutoImport] = []
        self.earlier = earlier or set()
        #: A link took the archive between the pass's read and its record.
        self.owned = owned

    async def known(self, archives: Any) -> set[int]:
        return set()

    async def slugs_by_hash(self, sha256: str) -> set[str]:
        return self.earlier

    async def record(self, row: AutoImport, *, plate_id: int | None = None) -> bool:
        if self.owned:
            return False
        self.rows.append(row)
        return True


class Client:
    def __init__(self, *archives: ArchiveDetail) -> None:
        self.archives = archives

    async def archive_outcomes(self, **_: Any) -> list[ArchiveDetail]:
        return list(self.archives)


def detail(archive_id: int, **fields: Any) -> ArchiveDetail:
    values: dict[str, Any] = {
        "id": archive_id,
        "printer_id": 1,
        "print_name": "demo",
        "filename": "demo.gcode.3mf",
        "content_hash": "ab" * 32,
        "created_at": NOW.replace(tzinfo=None),
        **fields,
    }
    return ArchiveDetail.model_validate(values)


async def _pass(
    monkeypatch: pytest.MonkeyPatch,
    archives: list[ArchiveDetail],
    *,
    sources: list[FoundSource],
    links: Links | None = None,
    store: Store | None = None,
    slugs: list[str] | None = None,
    settings: StoredSettings | None = None,
    printer: Printer | None = None,
    library_prints: list[int] | None = None,
    source_reads: list[int] | None = None,
) -> tuple[sweep.PassResult, Store]:
    async def no_link(*_: Any, library_prints: bool = True) -> list[Any]:
        assert not library_prints, "the pass links the library prints once itself"
        return []

    async def link_library(*_: Any) -> None:
        if library_prints is not None:
            library_prints.append(1)

    async def listed(client: Any, archive: ArchiveDetail, *, printer: bool) -> list[FoundSource]:
        assert not printer
        if source_reads is not None:
            source_reads.append(archive.id)
        return sources

    monkeypatch.setattr(sweep, "link_archive", no_link)
    monkeypatch.setattr(sweep, "find_sources", listed)
    monkeypatch.setattr(sweep, "link_library_prints", link_library)
    if printer is not None:
        printer.install(monkeypatch)
    store = store or Store()

    async def all_slugs() -> list[str]:
        return slugs if slugs is not None else ["demo", "cup"]

    async def exists(slug: str) -> bool:
        return True

    importer = AutoImporter(
        Client(*archives),  # type: ignore[arg-type]
        settings or StoredSettings(print_auto_import=True),
        links=links or Links(),  # type: ignore[arg-type]
        uploads=None,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        slugs=all_slugs,
        exists=exists,
    )
    return await importer.run(NOW - timedelta(minutes=5)), store


async def test_a_job_named_as_one_model_is_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    result, store = await _pass(
        monkeypatch, [detail(1)], sources=[found("archive", name="demo.gcode.3mf")]
    )
    assert (result.linked, result.inbox) == (1, 0)
    (row,) = store.rows
    assert (row.state, row.slug, row.matched_by, row.source) == (
        "linked",
        "demo",
        "job_name",
        "archive",
    )


async def test_an_unsure_name_goes_to_the_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    result, store = await _pass(
        monkeypatch,
        [detail(1, print_name="demo_elan")],
        sources=[found("archive", name="demo_elan.gcode.3mf")],
    )
    assert (result.linked, result.inbox) == (0, 1)
    (row,) = store.rows
    assert (row.state, row.slug, row.reason, row.suggested) == ("inbox", None, NO_MATCH, ["demo"])


async def test_an_earlier_import_of_the_file_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _, store = await _pass(
        monkeypatch,
        [detail(1, print_name="anything")],
        sources=[found("archive", name="x.gcode.3mf")],
        store=Store(earlier={"trophy"}),
    )
    (row,) = store.rows
    assert (row.slug, row.matched_by) == ("trophy", "earlier_import")


async def test_two_earlier_models_are_not_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _, store = await _pass(
        monkeypatch,
        [detail(1, print_name="anything")],
        sources=[found("archive", name="x.gcode.3mf")],
        store=Store(earlier={"trophy", "cup"}),
    )
    assert store.rows[0].state == "inbox"


async def test_a_match_with_no_file_still_goes_to_the_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = await _pass(
        monkeypatch, [detail(1)], sources=[FoundSource(kind="archive", available=False, reason="x")]
    )
    (row,) = store.rows
    assert (row.state, row.source, row.suggested) == ("inbox", None, ["demo"])
    assert row.reason is not None and row.reason.startswith("no source had the 3MF")


async def test_old_skipped_unprinted_and_busy_archives_are_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = detail(1, created_at=(NOW - timedelta(hours=1)).replace(tzinfo=None))
    upload = detail(2, printer_id=None)
    skipped = detail(3, printer_id=9)
    busy = detail(4)
    result, store = await _pass(
        monkeypatch,
        [old, upload, skipped, busy],
        sources=[found("archive", name="demo.gcode.3mf")],
        links=Links(busy={4}),
        settings=StoredSettings(print_auto_import=True, print_auto_import_skip_printers=[9]),
    )
    assert store.rows == []
    assert (result.linked, result.inbox, result.skipped) == (0, 0, 1)


# --- the library prints, once a pass ------------------------------------------------


async def test_the_library_prints_are_linked_once_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    await _pass(
        monkeypatch,
        [detail(1), detail(2, print_name="other")],
        sources=[found("archive", name="demo.gcode.3mf")],
        library_prints=calls,
    )
    assert calls == [1]


async def test_one_failing_archive_does_not_stop_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(Store):
        async def record(self, row: AutoImport, *, plate_id: int | None = None) -> bool:
            if row.archive_id == 1:
                raise RuntimeError("Bambuddy said no")
            return await super().record(row, plate_id=plate_id)

    result, store = await _pass(
        monkeypatch,
        [detail(1), detail(2)],
        sources=[found("archive", name="demo.gcode.3mf")],
        store=Failing(),
    )
    assert [row.archive_id for row in store.rows] == [2]
    assert (result.linked, result.skipped) == (1, 1)


# --- the printer's storage ----------------------------------------------------------

PRINTER: Any = ("archive", "archive_source", "library", "printer")
PRINTED = b"PK printed bytes"
PRINTED_HASH = hashlib.sha256(PRINTED).hexdigest()


class Printer:
    """The printer's storage (`printer_sources`, `read_source`) and the library inbox
    folder (`keep_printer_file`, `forget_copy`), faked."""

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        uploaded: bool = True,
        read_fails: dict[str, ApiError] | None = None,
        keep_fails: ApiError | None = None,
    ) -> None:
        self.files = files
        self.uploaded = uploaded
        #: The paths whose read raises, and the upload's error.
        self.read_fails = read_fails or {}
        self.keep_fails = keep_fails
        self.read: list[str] = []
        self.kept: list[SourceFile] = []
        self.forgotten: list[int] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def listing(client: Any, archive: ArchiveDetail) -> list[FoundSource]:
            if not self.files:
                return [FoundSource(kind="printer", available=False, reason="no such file")]
            return [
                FoundSource(kind="printer", available=True, name=path.rsplit("/", 1)[-1], path=path)
                for path in self.files
            ]

        async def read(client: Any, archive: ArchiveDetail, source: FoundSource) -> SourceFile:
            assert source.path is not None
            self.read.append(source.path)
            if source.path in self.read_fails:
                raise self.read_fails[source.path]
            data = self.files[source.path]
            return SourceFile(
                name=source.name or "x.3mf", data=data, sha256=hashlib.sha256(data).hexdigest()
            )

        async def keep(client: Any, file: SourceFile, folder: int) -> tuple[int, bool]:
            if self.keep_fails is not None:
                raise self.keep_fails
            self.kept.append(file)
            return 55, self.uploaded

        async def forget(client: Any, file_id: int) -> None:
            self.forgotten.append(file_id)

        monkeypatch.setattr(sweep, "printer_sources", listing)
        monkeypatch.setattr(sweep, "read_source", read)
        monkeypatch.setattr(sweep, "keep_printer_file", keep)
        monkeypatch.setattr(sweep, "forget_copy", forget)


NO_BAMBUDDY_SOURCE = [FoundSource(kind="archive", available=False, reason="gone")]


def printer_settings(**fields: Any) -> StoredSettings:
    values: dict[str, Any] = {
        "print_auto_import": True,
        "print_auto_import_sources": PRINTER,
        "library_folder_id": 2,
        **fields,
    }
    return StoredSettings.model_validate(values)


async def test_the_printed_file_is_read_kept_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = Printer({"/cache/demo.gcode.3mf": PRINTED})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    (row,) = store.rows
    assert (row.state, row.source, row.printer_path, row.library_file_id) == (
        "linked",
        "printer",
        "/cache/demo.gcode.3mf",
        55,
    )
    assert (row.sha256, row.size) == (PRINTED_HASH, len(PRINTED))
    assert [file.sha256 for file in printer.kept] == [PRINTED_HASH]
    assert printer.forgotten == []


async def test_a_file_of_the_same_name_with_other_bytes_is_never_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #6002: a newer file of the print's name took its place in the cache."""
    printer = Printer({"/cache/demo.gcode.3mf": b"PK a newer print"})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    (row,) = store.rows
    assert (row.state, row.source) == ("inbox", None)
    assert row.reason is not None and "is not the printed file" in row.reason
    assert printer.kept == []


async def test_the_matching_file_among_several_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    printer = Printer({"/demo.gcode.3mf": b"PK other", "/cache/demo.gcode.3mf": PRINTED})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    assert store.rows[0].printer_path == "/cache/demo.gcode.3mf"
    assert [file.sha256 for file in printer.kept] == [PRINTED_HASH]


async def test_an_archive_with_no_hash_takes_no_printer_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = Printer({"/cache/demo.gcode.3mf": PRINTED})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=None)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    (row,) = store.rows
    assert row.state == "inbox" and row.reason is not None and "gave no hash" in row.reason
    assert printer.read == [] and printer.kept == []


async def test_a_copy_is_deleted_when_the_archive_was_owned_meanwhile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = Printer({"/cache/demo.gcode.3mf": PRINTED})
    result, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
        store=Store(owned=True),
    )
    assert store.rows == [] and result.skipped == 1
    assert printer.forgotten == [55]


async def test_a_copy_found_in_the_inbox_already_is_not_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = Printer({"/cache/demo.gcode.3mf": PRINTED}, uploaded=False)
    await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
        store=Store(owned=True),
    )
    assert printer.forgotten == []


async def test_no_inbox_folder_says_so_and_reads_no_printer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = Printer({"/cache/demo.gcode.3mf": PRINTED})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(library_folder_id=None),
        printer=printer,
    )
    (row,) = store.rows
    assert row.state == "inbox" and row.reason is not None
    assert "library inbox folder" in row.reason
    assert printer.read == []


async def test_a_printer_with_no_file_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    printer = Printer({})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    (row,) = store.rows
    assert row.state == "inbox" and row.reason is not None
    assert "printer: no such file" in row.reason
    assert printer.kept == []


async def test_a_printer_read_that_fails_writes_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """#6011: the printer's file is readable only while the job runs; a read that failed
    now is tried again on the next pass, never recorded for good."""
    path = "/cache/demo.gcode.3mf"
    printer = Printer({path: PRINTED}, read_fails={path: ApiError(502, "FTP timed out")})
    result, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    assert store.rows == [] and result.skipped == 1
    assert printer.kept == []


async def test_a_failed_read_does_not_hide_another_file_that_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = Printer(
        {"/demo.gcode.3mf": b"PK other", "/cache/demo.gcode.3mf": PRINTED},
        read_fails={"/demo.gcode.3mf": ApiError(502, "FTP timed out")},
    )
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    assert store.rows[0].printer_path == "/cache/demo.gcode.3mf"


async def test_a_printer_file_that_is_no_3mf_goes_to_the_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file read that no retry changes stays a permanent answer."""
    path = "/cache/demo.gcode.3mf"
    printer = Printer({path: PRINTED}, read_fails={path: ApiError(422, "x is not a 3MF")})
    _, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    (row,) = store.rows
    assert (row.state, row.source) == ("inbox", None)
    assert row.reason is not None and "not a 3MF" in row.reason


async def test_a_printer_file_whose_upload_fails_writes_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6011: a failed upload to the library inbox folder is tried again next pass."""
    printer = Printer(
        {"/cache/demo.gcode.3mf": PRINTED}, keep_fails=ApiError(502, "Bambuddy did not answer")
    )
    result, store = await _pass(
        monkeypatch,
        [detail(1, content_hash=PRINTED_HASH)],
        sources=NO_BAMBUDDY_SOURCE,
        settings=printer_settings(),
        printer=printer,
    )
    assert store.rows == [] and result.skipped == 1


async def test_an_archive_taken_while_the_pass_waited_reads_no_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6011: both Schedules run the pass. The other run took the archive after this
    run's first `known` read; under the lock, this run checks again and reads nothing."""

    class TakenMeanwhile(Store):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def known(self, archives: Any) -> set[int]:
            self.calls += 1
            return set() if self.calls == 1 else {archive.id for archive in archives}

    reads: list[int] = []
    store = TakenMeanwhile()
    result, _ = await _pass(
        monkeypatch,
        [detail(1)],
        sources=[found("archive", name="demo.gcode.3mf")],
        store=store,
        source_reads=reads,
    )
    assert store.calls == 2
    assert reads == []
    assert store.rows == [] and result.skipped == 1
