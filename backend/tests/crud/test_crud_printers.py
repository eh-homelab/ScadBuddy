"""The print worker's printer registry, LAN reports and access codes through its CRUD
routes (#5298 Task 29, inventory Q22-Q26; `POST /printers/ensure-bambuddy` was taken
early by #4625). A run's first check makes or merges a Bambuddy printer's row on the
API, so a merge rewrites every table `PRINTER_REFERENCES` names (the machine rate's, the
settings and the activity rows among them) with the API's privileges, never the
worker's. `RemotePrinterStore` and `RemoteReports` answer as the SQL stores do, a write
sent twice leaves what one left (a report's `put` and a virtual printer's sync answer the
first write's answer under their key), another caller's credential is refused, the
access code is ``no-store`` and never logged, and the methods only the API calls are
refused on the worker."""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.secrets import SealError
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient
from scadbuddy.crud.print import (
    ACCESS_CODE_UNREADABLE,
    API_NEEDS_KEK,
    BASE,
    NO_ACCESS_CODE,
    NO_SUCH_PRINTER,
    PRINTERS_API_ONLY,
    PRINTERS_COMPOSED,
    ApiOnlyError,
    RemotePrinterStore,
    RemoteReports,
)
from scadbuddy.crud.wire import CRUD_HEADERS, NOT_CONFIGURED
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.machine_rate import CostProfile, MachineRateInputs, MachineRateStore
from scadbuddy.printers.models import BambuddyView, LanLink, PrinterCreate
from scadbuddy.printers.reports import Advance, PrintEnd, PrinterReportStore
from scadbuddy.printers.store import (
    KekMissingError,
    NoAccessCodeError,
    PrinterNotFoundError,
    PrinterStore,
)
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import FixedToken, threaded_print_crud
from tests.printers.conftest import KEK

SERIAL = "01P00A000000001"
CODE = "87654321"
CERT = "ab" * 32
UNKNOWN = "00000000-0000-4000-8000-0000000000ff"


def _public(cls: type) -> set[str]:
    """Its methods, but the ``_in`` ones, which run on a connection the worker has none of."""
    return {
        name
        for name, _ in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_") and not name.endswith("_in")
    }


# -- with no database --------------------------------------------------------------------


def test_the_areas_are_served_and_awaited() -> None:
    for area in ("printer-ensure", "printers"):
        assert area in CRUD_AREAS["print"]
        assert area in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's; Task 30's ``rack``
    # came after it.
    assert CRUD_AREAS["print"][-2:] == ["printers", "rack"]


def test_every_printer_method_is_remote_refused_or_named() -> None:
    """No `PrinterStore` method reaches the worker's SQL unnoticed: each is overridden
    over the routes, refused as the API's, or named as one that only calls those."""
    public = _public(PrinterStore)
    remote = {name for name in public if name in vars(RemotePrinterStore)}
    assert set(PRINTERS_API_ONLY) <= remote
    assert public - remote == set(PRINTERS_COMPOSED)


def test_every_report_method_is_remote() -> None:
    public = _public(PrinterReportStore)
    assert public == {name for name in public if name in vars(RemoteReports)}


def test_the_worker_store_overrides_every_method_that_merges_rows() -> None:
    """A row merge moves another service's rows (#4625): each PrinterStore method that
    merges is the API's, so the worker's store overrides it."""
    merging = {
        name
        for name, method in inspect.getmembers(PrinterStore, inspect.isfunction)
        if not name.startswith("_")
        and (
            "self._merge(" in inspect.getsource(method)
            or "self._ensure_locked(" in inspect.getsource(method)
        )
    }
    assert merging >= {"ensure_bambuddy_written", "link_bambuddy"}
    assert merging <= set(vars(RemotePrinterStore))


@pytest.mark.parametrize("name", PRINTERS_API_ONLY)
def test_a_printer_method_only_the_api_calls_is_refused(name: str) -> None:
    remote = RemotePrinterStore(None, None)  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        getattr(remote, name)("x")


def test_neither_worker_store_holds_a_pool() -> None:
    with pytest.raises(ApiOnlyError):
        _ = RemotePrinterStore(None, None).pool  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        _ = RemoteReports(None).pool  # type: ignore[arg-type]


def _answering(status: int, body: dict[str, Any]) -> CrudSyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=CRUD_HEADERS)

    return CrudSyncClient(
        "http://api",
        "print",
        ServiceAuth(FixedToken()),
        transport=httpx.MockTransport(answer),
        unavailable_budget=0.1,
    )


@pytest.mark.parametrize(
    ("status", "reason", "raised"),
    [
        (404, NO_ACCESS_CODE, NoAccessCodeError),
        (404, NO_SUCH_PRINTER, PrinterNotFoundError),
        (503, NOT_CONFIGURED, KekMissingError),
        (409, ACCESS_CODE_UNREADABLE, SealError),
    ],
)
def test_an_access_code_refusal_is_raised_as_the_sql_store_raises_it(
    status: int, reason: str, raised: type[Exception]
) -> None:
    client = _answering(status, {"status": status, "detail": "no", "reason": reason})
    try:
        with pytest.raises(raised) as error:
            RemotePrinterStore(client, None).open_access_code(UNKNOWN)
    finally:
        client.close()
    if raised is KekMissingError:
        # The worker's own key is no use to it: it is the API that lacks one.
        assert str(error.value) == API_NEEDS_KEK


@pytest.mark.parametrize(
    ("body", "advance"),
    [
        ({"ended": None, "started": "plate_2"}, Advance(started="plate_2")),
        (
            {"ended": {"subtask_name": "plate_1", "timed": True}, "started": None},
            Advance(ended=PrintEnd("plate_1", True)),
        ),
        # A ledger row written before the route answered the start (#4699 phase 3).
        ({"ended": None}, Advance()),
    ],
)
def test_a_report_write_answers_its_end_and_its_start(
    body: dict[str, Any], advance: Advance
) -> None:
    """The watch's nudge reads the start from the route's answer (#4699 phase 3), so the
    worker's `advance` is the route's, never the SQL store's."""
    client = _answering(200, body)
    try:
        assert RemoteReports(client).advance(UNKNOWN, {}, datetime.now(UTC)) == advance
    finally:
        client.close()


def test_a_name_that_is_no_printer_id_asks_nothing() -> None:
    client = _answering(500, {})
    try:
        remote = RemotePrinterStore(client, None)
        assert remote.get("not-a-uuid") is None
        with pytest.raises(PrinterNotFoundError):
            remote.open_access_code("not-a-uuid")
        reports = RemoteReports(client)
        assert reports.put("not-a-uuid", {}, datetime.now(UTC)) is None
        assert reports.advance("not-a-uuid", {}, datetime.now(UTC)) == Advance()
        assert reports.get("not-a-uuid") is None
    finally:
        client.close()


# -- over the routes ---------------------------------------------------------------------


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
def printers(store: SettingsStore) -> PrinterStore:
    """The API's registry, with the API's KEK."""
    return PrinterStore(store.pool, KEK)


@pytest.fixture
def reports(store: SettingsStore) -> PrinterReportStore:
    return PrinterReportStore(store.pool)


@pytest.fixture
def crud(store: SettingsStore, printers: PrinterStore) -> Iterator[CrudSyncClient]:
    with threaded_print_crud(store, printers=printers) as client:
        yield client


def _lan_printer(printers: PrinterStore, serial: str = SERIAL) -> str:
    row = printers.create(PrinterCreate(serial=serial, name="Garage"))
    printers.link_lan(row.id, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    return row.id


def _rows(store: SettingsStore, table: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return sorted((dict(row) for row in rows), key=lambda row: repr(sorted(row.items())))


@pytest.mark.requires_postgres
def test_the_reads_answer_as_the_sql_store(
    crud: CrudSyncClient, printers: PrinterStore, reports: PrinterReportStore
) -> None:
    lan = _lan_printer(printers)
    linked = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    reports.put(lan, {"gcode_state": "IDLE"}, datetime.now(UTC))
    remote = RemotePrinterStore(crud, None)
    remote_reports = RemoteReports(crud)
    assert remote.list() == printers.list()
    assert remote.get(lan) == printers.get(lan)
    assert remote.get(UNKNOWN) is None
    assert remote.require(lan) == printers.require(lan)
    assert remote.by_serial(SERIAL) == printers.by_serial(SERIAL)
    assert remote.by_serial("nobody") is None
    assert remote.by_bambuddy_id(4) == printers.by_bambuddy_id(4) == linked
    assert remote.by_bambuddy_id(5) is None
    assert remote_reports.get(lan) == reports.get(lan)
    assert remote_reports.get(linked.id) is None
    assert remote_reports.all() == reports.all()


@pytest.mark.requires_postgres
def test_the_workers_merge_runs_on_the_api(store: SettingsStore, printers: PrinterStore) -> None:
    """A synthetic row merges into the row holding the serial: its machine rate, the
    default printer setting and its runs' activity facets move with it, all on the API
    (the worker's role holds none of those tables)."""
    rates = MachineRateStore(store.pool)
    gone = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    inputs = MachineRateInputs(profile=CostProfile(watts="300"))
    rates.replace(gone.id, inputs)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('default_printer', %s)"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
            (Jsonb(gone.id),),
        )
        conn.execute(
            "INSERT INTO activity (id, kind, status, title, source, source_id, facets)"
            " VALUES ('print_run:r1', 'print_run', 'running', 'r1', 'print_run', 'r1', %s)",
            (Jsonb({"printer": gone.id}),),
        )
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    with threaded_print_crud(store, printers=printers) as client:
        worker = RemotePrinterStore(client, None)
        row, written = worker.ensure_bambuddy_written(
            4, serial=SERIAL, name="H2C", model="H2C", listed={4}
        )
        assert row.id == mine.id and row.bambuddy_printer_id == 4 and written
        assert printers.get(gone.id) is None
        assert rates.stored(mine.id).profile == inputs.profile
        with store.pool.connection() as conn:
            default = conn.execute(
                "SELECT value FROM settings WHERE name = 'default_printer'"
            ).fetchone()
            facets = conn.execute(
                "SELECT facets FROM activity WHERE id = 'print_run:r1'"
            ).fetchone()
        assert default is not None and default["value"] == mine.id
        assert facets is not None and facets["facets"]["printer"] == mine.id
        # Sent again, it finds the row and writes nothing (natural).
        before = _rows(store, "printers")
        again, wrote = worker.ensure_bambuddy_written(4, serial=SERIAL, name="H2C", model="H2C")
        assert again.id == mine.id and not wrote
        assert _rows(store, "printers") == before


@pytest.mark.requires_postgres
def test_the_access_code_is_released_unstored_and_never_logged(
    crud: CrudSyncClient,
    printers: PrinterStore,
    store: SettingsStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    pid = _lan_printer(printers)
    assert RemotePrinterStore(crud, None).open_access_code(pid) == CODE
    response = crud.request("GET", f"{BASE}/printers/{pid}/access-code")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-ScadBuddy-Crud"] == "1"
    [credential] = [c for c in ServiceCredentialStore(store.pool).list() if c.caller == "print"]
    released = [
        r.getMessage()
        for r in caplog.records
        if r.name == "scadbuddy.api.crud_print" and r.levelno == logging.INFO
    ]
    assert (
        released
        == [f"released printer {pid}'s access code to service credential {credential.id}"] * 2
    )
    assert CODE not in caplog.text


@pytest.mark.requires_postgres
def test_an_access_code_the_api_cannot_answer_is_refused(
    store: SettingsStore, printers: PrinterStore
) -> None:
    unlinked = printers.create(PrinterCreate(serial=SERIAL, name="Bare")).id
    lan = _lan_printer(printers, serial="01P00A000000002")
    with threaded_print_crud(store, printers=printers) as client:
        remote = RemotePrinterStore(client, None)
        with pytest.raises(NoAccessCodeError):
            remote.open_access_code(unlinked)
        with pytest.raises(PrinterNotFoundError):
            remote.open_access_code(UNKNOWN)
    # An API with no KEK opens nothing, and says it is the API's key that is missing.
    with (
        threaded_print_crud(store, printers=PrinterStore(store.pool, None)) as client,
        pytest.raises(KekMissingError) as missing,
    ):
        RemotePrinterStore(client, KEK).open_access_code(lan)
    assert str(missing.value) == API_NEEDS_KEK
    assert CODE not in str(missing.value)


def _report(state: str, name: str = "plate_1") -> dict[str, Any]:
    return {"subtask_name": name, "gcode_state": state}


@pytest.mark.requires_postgres
def test_a_report_put_sent_again_with_its_key_answers_the_same_end(
    crud: CrudSyncClient, printers: PrinterStore, reports: PrinterReportStore
) -> None:
    """Only the write that shows a print ended answers that end (Q26): a resend after a
    lost answer, under its key, answers it again rather than nothing."""
    pid = _lan_printer(printers)
    remote = RemoteReports(crud)
    start = datetime.now(UTC)
    assert remote.put(pid, _report("RUNNING"), start) is None
    body = {"report": _report("FINISH"), "seen_at": (start + timedelta(seconds=5)).isoformat()}
    key = uuid.uuid4().hex
    url = f"{BASE}/reports/{pid}"
    first = crud.request("PUT", url, json=body, idempotency_key=key).json()
    assert first == {"ended": {"subtask_name": "plate_1", "timed": True}, "started": None}
    seen = reports.get(pid)
    assert crud.request("PUT", url, json=body, idempotency_key=key).json() == first
    assert reports.get(pid) == seen
    # Without the key's ledger the same write ends nothing: the print ended already.
    assert remote.put(pid, _report("FINISH"), start + timedelta(seconds=5)) is None
    later = start + timedelta(seconds=10)
    # The write that shows a print start answers it, for the print watcher's nudge.
    assert remote.advance(pid, _report("RUNNING", "plate_2"), later) == Advance(started="plate_2")
    assert remote.put(pid, _report("FAILED", "plate_2"), later) == PrintEnd("plate_2", True)
    # A write that lands late, older than the row, moves nothing back (#5298 Task 30).
    assert remote.put(pid, _report("RUNNING", "plate_2"), start) is None
    assert reports.get(pid) == (_report("FAILED", "plate_2"), later)
    with pytest.raises(CrudRefusedError) as refused:
        crud.request("PUT", url, json=body)
    assert refused.value.status == 428


@pytest.mark.requires_postgres
def test_each_natural_write_sent_twice_leaves_what_one_left(
    crud: CrudSyncClient, printers: PrinterStore, reports: PrinterReportStore, store: SettingsStore
) -> None:
    pid = _lan_printer(printers)
    remote = RemoteReports(crud)
    now = datetime.now(UTC)
    reports.put(pid, {"gcode_state": "IDLE"}, now)

    def twice(write: Any) -> None:
        write()
        once = _rows(store, "printer_reports"), _rows(store, "printers")
        write()
        assert (_rows(store, "printer_reports"), _rows(store, "printers")) == once

    twice(lambda: remote.touch(pid, now + timedelta(seconds=10)))
    # Never moved back by an older touch.
    remote.touch(pid, now)
    seen = reports.get(pid)
    assert seen is not None and seen[1] == now + timedelta(seconds=10)
    twice(
        lambda: RemotePrinterStore(crud, None).ensure_bambuddy_written(
            9, serial=None, name="P1S", model="P1S"
        )
    )


@pytest.mark.requires_postgres
def test_a_virtual_printers_sync_answers_its_first_write_under_its_key(
    crud: CrudSyncClient, printers: PrinterStore, store: SettingsStore
) -> None:
    remote = RemotePrinterStore(crud, None)
    view = BambuddyView(seen_at=datetime.now(UTC), name="VP7", mode="immediate")
    row, outcome, changed = remote.upsert_virtual(
        7, serial=None, name="VP7", model="X1C", mode="immediate", forwards_to=None, view=view
    )
    assert (outcome, changed) == ("created", True)
    assert printers.by_bambuddy_id(7) is None and printers.get(row.id) == row
    body = {
        "serial": None,
        "name": "VP7",
        "model": "X1C",
        "mode": "immediate",
        "forwards_to": None,
        "view": view.model_dump(mode="json"),
        "may_adopt": True,
    }
    key = uuid.uuid4().hex
    url = f"{BASE}/printers/virtual/7"
    first = crud.request("PUT", url, json=body, idempotency_key=key).json()
    assert (first["outcome"], first["changed"]) == ("already_linked", False)
    before = _rows(store, "printers")
    assert crud.request("PUT", url, json=body, idempotency_key=key).json() == first
    assert _rows(store, "printers") == before
    # Bambuddy no longer lists it: the row is kept, marked; a resend answers the same.
    listed_key = uuid.uuid4().hex
    listed_url = f"{BASE}/printers/virtual-listed"
    marked = crud.request("POST", listed_url, json={"listed": []}, idempotency_key=listed_key)
    assert marked.json() == {"changed": [row.id]}
    again = crud.request("POST", listed_url, json={"listed": []}, idempotency_key=listed_key)
    assert again.json() == marked.json()
    assert remote.mark_virtual_listed([]) == ()
    gone = printers.get(row.id)
    assert gone is not None and gone.bambuddy_active is False


@pytest.mark.requires_postgres
def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    pid = "00000000-0000-4000-8000-000000000001"
    with threaded_print_crud(store, caller="projects") as client:
        for method, path in (
            ("GET", f"{BASE}/printers"),
            ("GET", f"{BASE}/printers/by-serial?serial={SERIAL}"),
            ("GET", f"{BASE}/printers/by-bambuddy-id/4"),
            ("GET", f"{BASE}/printers/{pid}"),
            ("GET", f"{BASE}/printers/{pid}/access-code"),
            ("POST", f"{BASE}/printers/ensure-bambuddy"),
            ("PUT", f"{BASE}/printers/virtual/7"),
            ("POST", f"{BASE}/printers/virtual-listed"),
            ("GET", f"{BASE}/reports"),
            ("GET", f"{BASE}/reports/{pid}"),
            ("PUT", f"{BASE}/reports/{pid}"),
            ("POST", f"{BASE}/reports/{pid}/touch"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path
