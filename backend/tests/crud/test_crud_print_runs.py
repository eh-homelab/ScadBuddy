"""The print worker's runs, outcomes, operations and events through its CRUD routes
(#5298 Task 24, inventory Q5, Q9-Q14): `RemotePrintRunStore` answers as `PrintRunStore`
does, every write sent twice answers once and announces once (D5, D7), an outcome is
recorded once, and an event the worker posts is logged once by its id and NOTIFYed only
then, of the kinds it publishes only. Every route is the print caller's alone."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.api import crud_print
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.api.crud_print import EVENT_KIND_REFUSED
from scadbuddy.bambuddy.outcomes import PrintOutcomeStore
from scadbuddy.bambuddy.print_links import PrintLinkStore, PrintReprint
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRunError, PrintRunStore, read_latest
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import PrinterEvent, PrintEvent, PrintRunEvent
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.operations import OTHER_CALLERS_KIND, RemoteOperationStore
from scadbuddy.crud.print import (
    BASE,
    RemoteEvents,
    RemoteOutcomes,
    RemotePrintRunStore,
    RemoteReprints,
)
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import OperationStore
from scadbuddy.printers.lan_models import LanStopped
from scadbuddy.worker import PRINT_CRUD_AREAS
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import InsertInput
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_print_crud, threaded_print_crud

pytestmark = pytest.mark.requires_postgres

SUBJECT = PrintSubject.library(7)
SLUG = "library-7"
RESULT = PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[51])
FAILED = PrintRunError(status=502, title="Bad Gateway", detail="Bambuddy said no")


async def _never(*args: Any) -> dict[str, Any]:
    raise AssertionError("a kind's check or run is the worker's, never the route's")


KINDS = {
    "reprint": OperationKind("reprint", _never, _never, queue="bambuddy"),
    "flow.test": OperationKind("flow.test", _never, _never, queue="projects"),
}


def settled(stage: str = "done") -> PrintProgress:
    return PrintProgress(
        route="slice_queue",
        stage=stage,  # type: ignore[arg-type]
        settled=True,
        queue_item_id=51,
        bambuddy_url="http://bambuddy.test/queue",
    )


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
async def bus(pg_conninfo: str, store: SettingsStore) -> AsyncIterator[PgNotifyEventBus]:
    """The API's bus, started: it hears its own NOTIFYs, as every replica does."""
    started = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    await started.start()
    try:
        yield started
    finally:
        await started.aclose()


@pytest.fixture
def runs(store: SettingsStore, bus: PgNotifyEventBus) -> PrintRunStore:
    """The API's store: each write's event and activity row commit with it."""
    return PrintRunStore(store.pool, events=bus, activity=ActivityIndex(store.pool, events=bus))


@pytest.fixture
async def crud(
    store: SettingsStore, runs: PrintRunStore, bus: PgNotifyEventBus
) -> AsyncIterator[CrudClient]:
    async with served_print_crud(
        store,
        runs=runs,
        outcomes=PrintOutcomeStore(store.pool, bus),
        events=bus,
        operations=OperationStore(store.pool, events=bus),
        kinds=KINDS,
        links=PrintLinkStore(store.pool),
    ) as client:
        yield client


def _logged(store: SettingsStore, kind: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT event_id, payload FROM events WHERE kind = %s ORDER BY seq", (kind,)
        ).fetchall()


async def _insert(remote: RemotePrintRunStore, run_id: str, run: str = "w1") -> Any:
    return await remote.insert_accepted(
        run_id,
        subject=SUBJECT,
        key="k1",
        slug=SLUG,
        workflow_id="print-k1",
        workflow_run_id=run,
        retention=None,
    )


def test_the_worker_waits_for_every_area_it_calls() -> None:
    assert set(CRUD_AREAS["print"]) >= PRINT_CRUD_AREAS >= {"runs", "operations", "events"}


async def test_an_insert_sent_twice_is_one_row_announced_once(
    crud: CrudClient, store: SettingsStore
) -> None:
    remote = RemotePrintRunStore(crud)
    first = await _insert(remote, "r1")
    # A retried activity mints a new run id for the same execution: its row answers.
    again = await _insert(remote, "r2")
    assert again == first and first.id == "r1" and first.status == "running"
    assert first.subject == SUBJECT.key and first.output_id == "library:7"
    assert [row["payload"]["run_id"] for row in _logged(store, "print.run")] == ["r1"]
    with store.pool.connection() as conn:
        activity = conn.execute("SELECT count(*) AS n FROM activity").fetchone()
    assert activity is not None and activity["n"] == 1


async def test_a_succeed_sent_twice_announces_once(crud: CrudClient, store: SettingsStore) -> None:
    remote = RemotePrintRunStore(crud)
    await _insert(remote, "r1")
    await remote.start_enqueue("r1")
    await remote.start_enqueue("r1")
    done = await remote.succeed("r1", SLUG, RESULT)
    again = await remote.succeed("r1", SLUG, RESULT)
    assert done == again and done.status == "succeeded" and done.result == RESULT
    # The insert's and the succeed's: the second succeed found the row ended.
    assert len(_logged(store, "print.run")) == 2
    # A fail after it changes nothing and announces nothing.
    assert (await remote.fail("r1", SLUG, FAILED)).status == "succeeded"
    assert (await remote.fail_lost("r1")).status == "succeeded"
    assert len(_logged(store, "print.run")) == 2


async def test_a_run_not_kept_is_a_lookup_error_or_none(crud: CrudClient) -> None:
    remote = RemotePrintRunStore(crud)
    with pytest.raises(LookupError):
        await remote.succeed("nope", SLUG, RESULT)
    with pytest.raises(LookupError):
        await remote.fail("nope", SLUG, FAILED)
    with pytest.raises(LookupError):
        await remote.update_result("nope", SLUG, RESULT)
    with pytest.raises(LookupError):
        await remote.fail_lost("nope")
    assert await remote.get("nope") is None
    assert (
        await remote.record_lan_stopped("nope", LanStopped(stage="cancelled", message="x")) is None
    )
    assert await remote.latest_for_output("nope") is None


async def test_the_reads_answer_as_the_sql_store(crud: CrudClient, runs: PrintRunStore) -> None:
    remote = RemotePrintRunStore(crud)
    inserted = await _insert(remote, "r1")
    assert await remote.get("r1") == inserted == await runs.get("r1")
    assert await remote.get_many(["r1", "nope"]) == {"r1": inserted}
    assert await remote.get_many([]) == {}
    assert await remote.latest_for_output("library:7") == inserted
    assert await remote.find("k1", has_request_id=True) == inserted
    assert await remote.find("k1", has_request_id=False) == inserted
    assert await remote.find("other", has_request_id=True) is None
    assert await remote.running_for("library:7") == [inserted]
    assert await remote.running_for_outputs(["library:7", "library:8"]) == {
        "library:7": [inserted],
        "library:8": [],
    }
    assert await remote.lan_waiting([]) == {}
    assert await remote.lan_waiting(["00000000-0000-0000-0000-000000000000"]) == {}
    failed = await remote.fail("r1", SLUG, FAILED, unqueued=True)
    assert failed == await runs.get("r1") and failed.error == FAILED
    assert not failed.may_have_queued


async def test_an_outcome_sent_twice_is_recorded_and_announced_once(
    crud: CrudClient, store: SettingsStore
) -> None:
    remote = RemotePrintRunStore(crud)
    outcomes = RemoteOutcomes(crud)
    await _insert(remote, "r1")
    run = await remote.succeed("r1", SLUG, RESULT)
    first = await outcomes.record("library:7", SLUG, settled())
    assert first is not None and (first.run_id, first.outcome) == ("r1", "finished")
    assert await outcomes.record("library:7", SLUG, settled()) is None
    assert await outcomes.record_run(run, SLUG, settled()) is None
    assert [row["payload"]["run_id"] for row in _logged(store, "print.outcome")] == ["r1"]
    # The newest succeeded run is the subject's own read's, never an older one.
    assert await outcomes.older_open("library:7") == []


async def test_an_older_runs_outcome_names_its_own_run(crud: CrudClient) -> None:
    remote = RemotePrintRunStore(crud)
    outcomes = RemoteOutcomes(crud)
    await _insert(remote, "r1")
    older = await remote.succeed("r1", SLUG, RESULT)
    await _insert(remote, "r2", run="w2")
    await remote.succeed("r2", SLUG, RESULT.model_copy(update={"queue_item_ids": [52]}))
    assert await outcomes.older_open("library:7") == [older]
    recorded = await outcomes.record_run(older, SLUG, settled("failed"))
    assert recorded is not None and (recorded.run_id, recorded.outcome) == ("r1", "failed")
    assert await outcomes.older_open("library:7") == []
    # A run of another subject is not this subject's to record.
    other = older.model_copy(update={"subject": "library:8"})
    assert await outcomes.record_run(other, SLUG, settled()) is None


async def test_an_event_posted_twice_is_one_row_and_one_notify(
    crud: CrudClient, store: SettingsStore, bus: PgNotifyEventBus
) -> None:
    heard = bus.subscribe(kinds=["print.progress"])
    events = RemoteEvents(crud)
    await events.start()
    event = PrintEvent(kind="print.progress", output_id="library:7", slug=SLUG)
    try:
        events.publish(event)
        events.publish(event)  # a resend: the same id
        # From another thread, as a store's `to_thread` body publishes.
        await asyncio.to_thread(
            events.publish, PrinterEvent(kind="printer.report", printer_id="p1")
        )
    finally:
        await events.aclose()
    assert [row["event_id"] for row in _logged(store, "print.progress")] == [event.id]
    assert len(_logged(store, "printer.report")) == 1
    async with asyncio.timeout(5):
        assert (await anext(heard)).id == event.id
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await anext(heard)
    heard.close()


async def test_a_kind_outside_the_allowlist_is_refused(
    crud: CrudClient, store: SettingsStore
) -> None:
    # A run's event is the API's to publish, with the row it describes.
    event = PrintRunEvent(output_id="library:7", slug=SLUG, run_id="r1")
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request("POST", f"{BASE}/events", json={"event": event.model_dump(mode="json")})
    assert refused.value.status == 403
    assert refused.value.problem["reason"] == EVENT_KIND_REFUSED
    assert _logged(store, "print.run") == []


async def test_the_print_worker_writes_its_own_kinds_operations(crud: CrudClient) -> None:
    operations = RemoteOperationStore(crud, "print")

    async def insert(kind: str) -> Any:
        return await operations.insert(
            "op-1",
            kind=kind,
            subject="s",
            operation_key="k",
            request={},
            workflow_id=f"op-{kind}-k",
            workflow_run_id="w1",
        )

    op = await insert("reprint")
    assert (op.kind, op.status) == ("reprint", "running")
    ended = await operations.finish(op.id, error=FAILED)
    assert ended.status == "failed"
    with pytest.raises(CrudRefusedError) as refused:
        await insert("flow.test")
    assert refused.value.problem["reason"] == OTHER_CALLERS_KIND


def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    with threaded_print_crud(store, caller="projects") as client:
        for method, path in (
            ("GET", f"{BASE}/runs/latest?output_id=x"),
            ("POST", f"{BASE}/outcomes"),
            ("POST", f"{BASE}/events"),
            ("POST", f"{BASE}/operations"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path


async def test_the_insert_prunes_past_the_apis_own_retention(
    crud: CrudClient, store: SettingsStore, runs: PrintRunStore
) -> None:
    remote = RemotePrintRunStore(crud)
    await _insert(remote, "r1")
    await remote.fail("r1", SLUG, FAILED)
    with store.pool.connection() as conn:
        conn.execute("UPDATE print_runs SET finished_at = now() - interval '2 days'")
    store.save(SettingsPatch(print_run_retention_seconds=timedelta(days=1).total_seconds()))
    # The worker's retention is not sent: the route reads the API's setting.
    await remote.insert_accepted(
        "r2",
        subject=SUBJECT,
        key="k2",
        slug=SLUG,
        workflow_id="print-k2",
        workflow_run_id="w1",
        retention=None,
    )
    assert await runs.get("r1") is None and await runs.get("r2") is not None


async def test_a_reprint_sent_twice_is_one_row(crud: CrudClient, store: SettingsStore) -> None:
    """#3741: the queue item a reprint queued, recorded through the API (#5295): sent
    again, the first record stays. It is not a send of the subject."""
    remote = RemoteReprints(crud)
    await remote.record_reprint(SUBJECT, PrintReprint(queue_item_id=61, archive_id=40))
    await remote.record_reprint(SUBJECT, PrintReprint(queue_item_id=61, archive_id=41))
    links = PrintLinkStore(store.pool)
    job = (await links.jobs(queue_items=[61])).queue_items[61]
    assert (job.subject, job.archive_id, job.run_id) == (SUBJECT.key, 40, None)
    assert await links.sends_for(SUBJECT) == []


async def test_an_api_out_of_reach_reads_as_no_run() -> None:
    """As a database that does not answer (`read_latest`): the follow reads the progress
    without the newest run, and is never failed by it."""

    async def unreachable(run_subject: str) -> Any:
        raise CrudUnavailableError("CRUD route unavailable", "unreached")

    assert await read_latest(unreachable, "library:7") is None


async def test_a_result_sent_again_announces_once(crud: CrudClient, store: SettingsStore) -> None:
    """A LAN run's rewrite sent again (a retried `print_lan_update`) finds the same result
    there: the same row, and no second ``print.run``."""
    remote = RemotePrintRunStore(crud)
    await _insert(remote, "r1")
    await remote.succeed("r1", SLUG, RESULT)
    later = RESULT.model_copy(update={"queue_item_ids": [51, 52]})
    first = await remote.update_result("r1", SLUG, later)
    again = await remote.update_result("r1", SLUG, later)
    assert first == again and first.result == later
    # The insert's, the succeed's and the first rewrite's.
    assert len(_logged(store, "print.run")) == 3


@pytest.mark.parametrize("kind", [["print.progress"], {"a": 1}, None, 7])
async def test_a_kind_that_is_not_a_string_is_refused(crud: CrudClient, kind: Any) -> None:
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request("POST", f"{BASE}/events", json={"event": {"kind": kind}})
    assert refused.value.status == 403
    assert refused.value.problem["reason"] == EVENT_KIND_REFUSED


def test_the_released_credentials_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings route's INFO-once log keeps the last `RELEASED_KEPT` credentials, not
    every one the process has seen."""
    monkeypatch.setattr(crud_print, "_released_to", OrderedDict())
    monkeypatch.setattr(crud_print, "RELEASED_KEPT", 3)
    assert crud_print._first_release("a") and not crud_print._first_release("a")
    for credential in ("b", "c", "d"):
        assert crud_print._first_release(credential)
    assert list(crud_print._released_to) == ["b", "c", "d"]
    # "a" fell out: it is logged at INFO once more.
    assert crud_print._first_release("a")
    # A seen one moves to the newest place, so it is the last to fall out.
    assert not crud_print._first_release("c")
    assert list(crud_print._released_to) == ["d", "a", "c"]


async def test_the_remote_insert_reads_no_settings() -> None:
    """`print_insert` over the CRUD routes: the API prunes past its own setting, so the
    activity spends no settings read on a retention it would not send."""

    class Runs:
        uses_retention = False

        async def insert_accepted(self, run_id: str, **fields: Any) -> Any:
            assert fields["retention"] is None
            return "inserted"

    class Unread:
        def load(self, timeout: float | None = None) -> Any:
            raise AssertionError("the settings were read")

    activities = PrintActivities(
        PrintDeps(
            settings_store=cast(Any, Unread()),
            outputs=cast(Any, None),
            prints=None,
            uploads=cast(Any, None),
            store=cast(Any, Runs()),
            observer=cast(Any, None),
        )
    )
    input = InsertInput.model_construct(
        input=SimpleNamespace(subject="library:7", key="k1", slug=SLUG),
        checked=SimpleNamespace(prepared=SimpleNamespace(printer=None)),
    )
    inserted: Any = await ActivityEnvironment().run(activities.insert, input)
    assert inserted == "inserted"
