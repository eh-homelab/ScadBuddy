"""#4699 — the print watcher's choices, without Postgres: which source it takes, which
model it links, and what goes to the inbox. The pass against Postgres and a mocked
Bambuddy is `tests/api/test_print_watch.py`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_import import FoundSource, exact_slugs
from scadbuddy.bambuddy.print_links import ImportBusyError
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
    def __init__(self, earlier: set[str] | None = None) -> None:
        self.rows: list[AutoImport] = []
        self.earlier = earlier or set()

    async def known(self, archives: Any) -> set[int]:
        return set()

    async def slugs_by_hash(self, sha256: str) -> set[str]:
        return self.earlier

    async def record(self, row: AutoImport, *, plate_id: int | None = None) -> bool:
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
) -> tuple[sweep.PassResult, Store]:
    async def no_link(*_: Any) -> list[Any]:
        return []

    async def listed(*_: Any, printer: bool) -> list[FoundSource]:
        assert not printer
        return sources

    monkeypatch.setattr(sweep, "link_archive", no_link)
    monkeypatch.setattr(sweep, "find_sources", listed)
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
