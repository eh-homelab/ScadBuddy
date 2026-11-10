"""The print worker's print links, sends (#5298 Task 28, inventory Q20) and outputs' last
prints (Q21) through its CRUD routes: `RemotePrintLinks` and `RemoteOutputPrints` answer
as `PrintLinkStore` and `OutputPrintStore` do, every write sent twice leaves what one
left, another caller's credential is refused, and the methods only the API calls are
refused on the worker. ``pending_library`` is one of them: only the prints list's
library linking pass (`linking.link_library_prints`, on the API) claims those rows."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore, PrintSend
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.print import (
    BASE,
    LINKS_API_ONLY,
    LINKS_COMPOSED,
    OUTPUT_PRINTS_API_ONLY,
    ApiOnlyError,
    RemoteOutputPrints,
    RemotePrintLinks,
)
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import LanPlateSend, PlateSend
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_print_crud, threaded_print_crud

OUTPUT = "a" * 32
OTHER = "b" * 32
PRINTER = "00000000-0000-4000-8000-000000000001"


def _public(cls: type) -> set[str]:
    return {
        name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")
    }


def test_the_worker_waits_for_the_links_area() -> None:
    assert "links" in PRINT_CRUD_AREAS
    assert "links" in CRUD_AREAS["print"]


def test_every_links_method_is_remote_refused_or_named() -> None:
    """No `PrintLinkStore` method reaches the worker's SQL unnoticed: each is overridden
    over the routes, refused as the API's, or named as one that only calls those."""
    public = _public(PrintLinkStore)
    remote = {name for name in public if name in vars(RemotePrintLinks)}
    assert set(LINKS_API_ONLY) <= remote
    assert "pending_library" in LINKS_API_ONLY
    assert public - remote == set(LINKS_COMPOSED)


def test_every_last_print_method_is_remote_or_refused() -> None:
    public = _public(OutputPrintStore)
    remote = {name for name in public if name in vars(RemoteOutputPrints)}
    assert set(OUTPUT_PRINTS_API_ONLY) <= remote
    assert public == remote


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def sql(store: SettingsStore) -> PrintLinkStore:
    """The API's own store, on the same schema."""
    return PrintLinkStore(store.pool)


@pytest.fixture
async def crud(store: SettingsStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store) as client:
        yield client


@pytest.fixture
def remote(crud: CrudClient) -> RemotePrintLinks:
    return RemotePrintLinks(crud)


def _rows(store: SettingsStore, table: str, *ignored: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return sorted(
        ({**row, **dict.fromkeys(ignored)} for row in rows),
        key=lambda row: repr(sorted(row.items())),
    )


def link(archive_id: int, queue_item_id: int | None = None) -> PrintLink:
    return PrintLink(
        archive_id=archive_id,
        matched_by="queue_item" if queue_item_id is not None else "content_hash",
        queue_item_id=queue_item_id,
        plate_id=1,
        printer_id=3,
    )


def send(queue_item_id: int, run_id: str | None = "r1") -> PrintSend:
    return PrintSend(queue_item_id=queue_item_id, plate_id=1, printer_id=3, run_id=run_id)


@pytest.mark.requires_postgres
async def test_the_reads_answer_as_the_sql_store(
    remote: RemotePrintLinks, sql: PrintLinkStore
) -> None:
    output = PrintSubject.output(OUTPUT)
    library = PrintSubject.library(7)
    await remote.record(output, link(101, 11))
    await remote.record(output, link(102))
    await remote.record(PrintSubject.output(OTHER), link(102))
    await remote.record_sends(output, [send(11), send(12, "r2")])
    await remote.record_sends(library, [send(21), send(22)])
    await remote.record_sends(library, [])

    assert await remote.for_subject(output) == await sql.for_subject(output)
    assert [found.archive_id for found in await remote.for_subject(output)] == [101, 102]
    assert await remote.linked(102) == await sql.linked(102)
    assert await remote.linked(999) is None
    assert await remote.output_for(101) == OUTPUT
    assert await remote.linked_queue_items(output) == {11}
    assert await remote.sends_for(output) == await sql.sends_for(output)
    assert await remote.run_sends(output, "r1") == await sql.run_sends(output, "r1")
    assert [found.queue_item_id for found in await remote.run_sends(output, "r1")] == [11]
    assert await remote.last_run(output) == await sql.last_run(output)
    assert await remote.sends_for(PrintSubject.output("c" * 32)) == []


@pytest.mark.requires_postgres
async def test_each_link_write_sent_twice_leaves_what_one_left(
    remote: RemotePrintLinks, sql: PrintLinkStore, store: SettingsStore
) -> None:
    """Natural writes (plan D5): a resend after a lost answer changes nothing."""

    async def twice(write: Any) -> None:
        await write()
        once = _rows(store, "print_links"), _rows(store, "print_sends")
        await write()
        assert (_rows(store, "print_links"), _rows(store, "print_sends")) == once

    output = PrintSubject.output(OUTPUT)
    library = PrintSubject.library(7)
    await twice(lambda: remote.record(output, link(101, 11)))
    await twice(lambda: remote.record_sends(output, [send(11)]))
    await twice(lambda: remote.record_sends(library, [send(21), send(22)]))
    await twice(lambda: remote.link_library(21, 201, "plate.gcode.3mf"))
    await twice(lambda: remote.library_gone(22))
    linked = await sql.linked(201)
    assert linked is not None and linked.library_file_id == 7
    assert linked.name == "plate.gcode.3mf"
    gone = {row["queue_item_id"]: row["gone"] for row in _rows(store, "print_sends")}
    assert gone == {11: False, 21: False, 22: True}


@pytest.mark.requires_postgres
def test_each_last_print_write_sent_twice_leaves_what_one_left(store: SettingsStore) -> None:
    sql = OutputPrintStore(store.pool)
    with threaded_print_crud(store) as client:
        remote = RemoteOutputPrints(client)

        def twice(write: Any) -> None:
            write()
            once = _rows(store, "output_last_prints", "recorded_at")
            write()
            assert _rows(store, "output_last_prints", "recorded_at") == once

        twice(
            lambda: remote.record(
                OUTPUT,
                queue_item_id=11,
                slice_job_id=9,
                project_id=None,
                plates=[PlateSend(plate_id=1, queue_item_id=11, slice_job_id=9)],
            )
        )
        recorded = sql.for_outputs([OUTPUT])[OUTPUT]
        assert recorded.queue_item_id == 11 and recorded.print_route == "slice_queue"
        plates = [LanPlateSend(plate_id=1, copies=2)]
        twice(lambda: remote.record_lan(OUTPUT, run_id="r1", printer=PRINTER, plates=plates))
        twice(lambda: remote.record_lan_progress(OUTPUT, "r1", stage="done", settled=True))
        twice(lambda: remote.record_lan(OUTPUT, run_id="r1", printer=PRINTER, plates=plates))
        lan = sql.for_outputs([OUTPUT])[OUTPUT]
        assert (lan.print_route, lan.print_run_id, lan.printer) == ("lan", "r1", PRINTER)
        # A retried record of the same run keeps the stage the follow wrote since.
        assert (lan.print_stage, lan.print_settled, lan.lan_plates) == ("done", True, plates)
        assert lan.sent_to_bambuddy


@pytest.mark.parametrize("name", LINKS_API_ONLY)
def test_a_links_method_only_the_api_calls_is_refused(name: str) -> None:
    remote = RemotePrintLinks(None)  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        getattr(remote, name)([])


@pytest.mark.parametrize("name", OUTPUT_PRINTS_API_ONLY)
def test_a_last_print_method_only_the_api_calls_is_refused(name: str) -> None:
    remote = RemoteOutputPrints(None)  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        getattr(remote, name)([])


@pytest.mark.requires_postgres
def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    with threaded_print_crud(store, caller="projects") as client:
        for method, path in (
            ("POST", f"{BASE}/links"),
            ("GET", f"{BASE}/links?subject=output:{OUTPUT}"),
            ("GET", f"{BASE}/links/1"),
            ("POST", f"{BASE}/sends"),
            ("GET", f"{BASE}/sends?subject=output:{OUTPUT}"),
            ("POST", f"{BASE}/sends/1/library-link"),
            ("POST", f"{BASE}/sends/1/library-gone"),
            ("PUT", f"{BASE}/last-prints/{OUTPUT}"),
            ("PUT", f"{BASE}/last-prints/{OUTPUT}/lan"),
            ("PUT", f"{BASE}/last-prints/{OUTPUT}/lan-progress"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path


@pytest.mark.requires_postgres
async def test_a_subject_the_api_cannot_read_is_refused(crud: CrudClient) -> None:
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request("GET", f"{BASE}/links?subject=nonsense")
    assert refused.value.status == 422
