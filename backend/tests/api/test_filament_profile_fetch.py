"""#6625 Task 4: the public routes that start and read a spool type's 3DFP preset fetch,
and the invoice confirm that starts one for each new type. Temporal is a fake client under
the real `profile_fetch.FetchStarter` (`profile_fetch.starter` patched), as the intake
tests patch `intake.checker`."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.api import filament as filament_api
from scadbuddy.filament import profile_fetch
from scadbuddy.printers.component import PRINTERS
from scadbuddy.profile_fetch.wire import FETCH_WORKFLOW, FetchAsk
from scadbuddy.profile_fetch.workflow import EXECUTION_TIMEOUT, LONGEST_RUN
from tests.api.test_crud_profile_fetch import _state

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
NOZZLES = ["0.2", "0.4", "0.6", "0.8"]


class FakeTemporal:
    """The workflows the API starts, as `Client.start_workflow` gets them: ``(workflow,
    id, task queue, ask)``, after checking the conflict and reuse policies. As Temporal
    does under USE_EXISTING, a start whose id names a workflow that still runs starts
    nothing and returns that workflow (``attached`` lists those asks); the fake's
    workflows never end. ``fail`` makes every start raise; ``hang`` makes every start
    wait ten seconds before it is taken (``in_flight`` counts the waiting starts, and
    ``most_in_flight`` the most at once)."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, str, FetchAsk]] = []
        self.attached: list[FetchAsk] = []
        self.fail = False
        self.hang = False
        self.in_flight = 0
        self.most_in_flight = 0
        #: Each start's ``execution_timeout``.
        self.execution_timeouts: list[timedelta | None] = []

    async def start_workflow(self, workflow: str, ask: FetchAsk, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("Temporal is down")
        if self.hang:
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
            try:
                await asyncio.sleep(10)
            finally:
                self.in_flight -= 1
        assert kwargs.pop("id_conflict_policy") == WorkflowIDConflictPolicy.USE_EXISTING
        assert kwargs.pop("id_reuse_policy") == WorkflowIDReusePolicy.ALLOW_DUPLICATE
        workflow_id = kwargs.pop("id")
        self.execution_timeouts.append(kwargs.pop("execution_timeout", None))
        if any(started[1] == workflow_id for started in self.started):
            self.attached.append(ask)
            kwargs.pop("task_queue")
        else:
            self.started.append((workflow, workflow_id, kwargs.pop("task_queue"), ask))
        assert kwargs == {}


class FakeProbe:
    """`profile_fetch.QueueProbe`: whether the profile-fetch queue has pollers, and the
    cancel of a run's workflow. ``fail`` makes the read raise, as a Temporal that does not
    answer; ``reads`` counts them. ``cancelled`` lists the runs whose workflow was
    cancelled; ``cancel_fail`` makes the cancel raise."""

    def __init__(self) -> None:
        self.pollers = True
        self.fail = False
        self.reads = 0
        self.cancelled: list[str] = []
        self.cancel_fail = False

    async def has_pollers(self) -> bool:
        self.reads += 1
        if self.fail:
            raise RuntimeError("Temporal is down")
        return self.pollers

    async def cancel(self, run_id: str) -> None:
        if self.cancel_fail:
            raise RuntimeError("Temporal is down")
        self.cancelled.append(run_id)


@pytest.fixture(autouse=True)
def probe(monkeypatch: pytest.MonkeyPatch) -> FakeProbe:
    fake = FakeProbe()
    monkeypatch.setattr(profile_fetch, "probe", lambda state: fake)
    return fake


@pytest.fixture(autouse=True)
def starter(monkeypatch: pytest.MonkeyPatch) -> FakeTemporal:
    fake = FakeTemporal()
    monkeypatch.setattr(
        profile_fetch,
        "starter",
        lambda state: profile_fetch.FetchStarter(
            fake,  # type: ignore[arg-type]
            state.settings.temporal_task_queue_profile_fetch,
        ),
    )
    return fake


def _printer(client: TestClient, model: str | None = "H2C") -> None:
    _state(client).components.get(PRINTERS).store.create_linked(
        serial="31B598000000", name="H2C", model=model, bambuddy_printer_id=1
    )


def _material(client: TestClient) -> str:
    return str(
        next(m["id"] for m in client.get(f"{URL}/catalog/materials").json() if m["name"] == "PLA")
    )


def _type_body(client: TestClient, code: str, tdfp_id: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "material_id": _material(client),
        "rgba": "#FFFFFF",
        "label_weight_g": 1000,
    }
    if tdfp_id is not None:
        body["tdfp_id"] = tdfp_id
    return body


def _type(client: TestClient, code: str = "PLA-WHT", tdfp_id: int | None = 30173) -> str:
    made = client.post(f"{URL}/types", json=_type_body(client, code, tdfp_id))
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _start(client: TestClient, type_id: str) -> Any:
    return client.post(f"{URL}/types/{type_id}/profile-fetch")


def test_start_opens_a_run_and_starts_the_workflow(
    client: TestClient, starter: FakeTemporal
) -> None:
    _printer(client)
    type_id = _type(client)
    r = _start(client, type_id)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["joined"] is False
    run = body["run"]
    assert run["status"] == "running"
    assert run["type_id"] == type_id
    assert run["tdfp_id"] == 30173
    [(workflow, workflow_id, queue, ask)] = starter.started
    assert (workflow, workflow_id, queue) == (
        FETCH_WORKFLOW,
        f"profile-fetch:{run['id']}",
        "profile-fetch",
    )
    assert ask.run_id == run["id"]
    assert ask.type_id == type_id
    assert ask.tdfp_id == 30173
    assert [t.model_dump() for t in ask.targets] == [{"printer_model": "H2C", "nozzles": NOZZLES}]
    # Temporal ends a workflow that outlives it; the API's last resort waits past it.
    assert starter.execution_timeouts == [EXECUTION_TIMEOUT] == [timedelta(hours=24)]
    assert EXECUTION_TIMEOUT + timedelta(hours=1) == LONGEST_RUN


def test_a_second_start_joins_the_running_fetch(client: TestClient, starter: FakeTemporal) -> None:
    _printer(client)
    type_id = _type(client)
    first = _start(client, type_id).json()
    again = _start(client, type_id)
    assert again.status_code == 202, again.text
    assert again.json()["joined"] is True
    assert again.json()["run"]["id"] == first["run"]["id"]
    assert len(starter.started) == 1


def test_an_ended_runs_workflow_does_not_capture_a_new_start(
    client: TestClient, starter: FakeTemporal
) -> None:
    _printer(client)
    type_id = _type(client)
    first = _start(client, type_id).json()["run"]
    # The run ends while its workflow still runs (Decision 25's lost answer, or the short
    # time after the workflow ended the run and before the workflow itself ends).
    profile_fetch.fetches(_state(client)).fail_unstarted(UUID(first["id"]))
    again = _start(client, type_id)
    assert again.status_code == 202, again.text
    assert again.json()["joined"] is False
    second = again.json()["run"]
    assert second["id"] != first["id"]
    assert starter.attached == []
    assert [ask.run_id for (_, _, _, ask) in starter.started] == [first["id"], second["id"]]


def test_targets_follow_the_nozzles_setting(client: TestClient, starter: FakeTemporal) -> None:
    _printer(client)
    saved = client.put("/api/v1/settings", json={"profile_fetch_nozzles": ["0.4", "0.6"]})
    assert saved.status_code == 200, saved.text
    r = _start(client, _type(client))
    assert r.status_code == 202, r.text
    assert r.json()["run"]["targets"] == [{"printer_model": "H2C", "nozzles": ["0.4", "0.6"]}]
    [(_, _, _, ask)] = starter.started
    assert ask.targets[0].nozzles == ["0.4", "0.6"]


def test_no_such_type_is_404(client: TestClient, starter: FakeTemporal) -> None:
    _printer(client)
    missing = "00000000-0000-4000-8000-000000000001"
    assert _start(client, missing).status_code == 404
    assert client.get(f"{URL}/types/{missing}/profile-fetch").status_code == 404
    assert starter.started == []


def test_no_tdfp_id_is_422(client: TestClient, starter: FakeTemporal) -> None:
    _printer(client)
    r = _start(client, _type(client, tdfp_id=None))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_tdfp_id"
    assert starter.started == []


def test_no_printer_model_is_422(client: TestClient, starter: FakeTemporal) -> None:
    _printer(client, model=None)
    r = _start(client, _type(client))
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_printer_models"
    assert starter.started == []


def test_a_failed_start_fails_the_run_and_answers_503(
    client: TestClient, starter: FakeTemporal
) -> None:
    _printer(client)
    type_id = _type(client)
    starter.fail = True
    r = _start(client, type_id)
    assert r.status_code == 503, r.text
    run = client.get(f"{URL}/types/{type_id}/profile-fetch").json()["run"]
    assert run["status"] == "failed"
    assert run["error"] == "the fetch could not be started"
    starter.fail = False
    again = _start(client, type_id)
    assert again.status_code == 202, again.text
    assert again.json()["joined"] is False
    assert again.json()["run"]["id"] != run["id"]


def test_a_hung_start_fails_the_run_and_answers_503(
    client: TestClient, starter: FakeTemporal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filament_api, "START_TIMEOUT_SECONDS", 0.5)
    _printer(client)
    type_id = _type(client)
    starter.hang = True
    began = time.monotonic()
    r = _start(client, type_id)
    assert r.status_code == 503, r.text
    assert time.monotonic() - began < 5
    run = client.get(f"{URL}/types/{type_id}/profile-fetch").json()["run"]
    assert run["status"] == "failed"
    assert run["error"] == "the fetch could not be started"


def test_status_lists_the_gaps(client: TestClient) -> None:
    _printer(client)
    type_id = _type(client)
    empty = client.get(f"{URL}/types/{type_id}/profile-fetch")
    assert empty.status_code == 200, empty.text
    assert empty.json()["run"] is None
    assert len(empty.json()["gaps"]) == 4
    put = client.put(
        f"{URL}/types/{type_id}/presets",
        json=[{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "101"}],
    )
    assert put.status_code == 200, put.text
    run = _start(client, type_id).json()["run"]
    read = client.get(f"{URL}/types/{type_id}/profile-fetch").json()
    assert read["run"]["id"] == run["id"]
    assert read["gaps"] == [
        {
            "printer_model": "H2C",
            "nozzle_diameter": n,
            "reason": "no preset for this printer and nozzle yet",
        }
        for n in ["0.2", "0.6", "0.8"]
    ]


# -- the invoice confirm ------------------------------------------------------------------


def _supplier(client: TestClient) -> str:
    made = client.post(f"{URL}/suppliers", json={"name": "Acme"})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _invoice(client: TestClient, lines: list[dict[str, Any]]) -> str:
    total = f"{20 * len(lines)}.00"
    made = client.post(
        f"{URL}/invoices",
        json={
            "supplier_id": _supplier(client),
            "number": "INV-1",
            "invoice_date": "2026-11-18",
            "currency": "USD",
            "subtotal": total,
            "shipping": "0",
            "tax": "0",
            "total": total,
            "lines": lines,
        },
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _line(**over: Any) -> dict[str, Any]:
    return {"description": "x", "quantity": 1, "unit_price": "20.00", **over}


def test_confirm_starts_a_fetch_for_each_new_type_with_a_tdfp_id(
    client: TestClient, starter: FakeTemporal
) -> None:
    _printer(client)
    existing = _type(client, "OLD", tdfp_id=111)
    invoice_id = _invoice(
        client,
        [
            _line(description="a", proposed_type=_type_body(client, "NEW-1", 30173)),
            _line(description="b", proposed_type=_type_body(client, "NEW-2", None)),
            _line(description="c", type_id=existing),
        ],
    )
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    types = {t["code"]: t["id"] for t in client.get(f"{URL}/types").json()}
    [(_, workflow_id, _, ask)] = starter.started
    assert ask.tdfp_id == 30173
    run = client.get(f"{URL}/types/{types['NEW-1']}/profile-fetch").json()["run"]
    assert run["id"] == ask.run_id
    assert workflow_id == f"profile-fetch:{run['id']}"
    for code in ("NEW-2", "OLD"):
        assert client.get(f"{URL}/types/{types[code]}/profile-fetch").json()["run"] is None
    again = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert again.status_code == 200
    assert len(starter.started) == 1


def test_a_start_failure_never_fails_the_confirm(
    client: TestClient, starter: FakeTemporal, caplog: pytest.LogCaptureFixture
) -> None:
    _printer(client)
    starter.fail = True
    invoice_id = _invoice(client, [_line(proposed_type=_type_body(client, "NEW-1", 30173))])
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "confirmed"
    assert "could not start the profile fetch" in caplog.text


def test_a_hung_start_never_holds_the_confirm(
    client: TestClient, starter: FakeTemporal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile_fetch, "START_TIMEOUT_SECONDS", 0.5)
    _printer(client)
    starter.hang = True
    invoice_id = _invoice(
        client,
        [
            _line(description="a", proposed_type=_type_body(client, "NEW-1", 30173)),
            _line(description="b", proposed_type=_type_body(client, "NEW-2", 30174)),
        ],
    )
    began = time.monotonic()
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    assert time.monotonic() - began < 5
    assert starter.most_in_flight == 2
    types = {t["code"]: t["id"] for t in client.get(f"{URL}/types").json()}
    for code in ("NEW-1", "NEW-2"):
        run = client.get(f"{URL}/types/{types[code]}/profile-fetch").json()["run"]
        assert run["status"] == "failed"
        assert run["error"] == "the fetch could not be started"


def test_no_printer_model_is_not_logged_without_a_fetchable_type(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=profile_fetch.__name__)
    _printer(client, model=None)
    invoice_id = _invoice(client, [_line(proposed_type=_type_body(client, "NEW-1", None))])
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    assert _fetch_records(caplog) == []


def test_no_printer_model_is_logged_once_per_confirm(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=profile_fetch.__name__)
    _printer(client, model=None)
    invoice_id = _invoice(
        client,
        [
            _line(description="a", proposed_type=_type_body(client, "NEW-1", 30173)),
            _line(description="b", proposed_type=_type_body(client, "NEW-2", 30174)),
        ],
    )
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    [warning] = _fetch_records(caplog)
    assert "no printer has a model" in warning.getMessage()


def _fetch_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The profile fetch's own records: ``caplog`` also holds every other logger's
    warnings, such as ``core/loop_watch.py``'s stall warning on a loaded runner."""
    return [r for r in caplog.records if r.name == profile_fetch.__name__]


# -- a run no worker serves (build Decision 62) ------------------------------------------


def _started_ago(client: TestClient, run_id: str, age: timedelta) -> None:
    with _state(client).settings_store.pool.connection() as conn:
        conn.execute(
            "UPDATE filament_profile_fetches SET started_at = now() - %s WHERE id = %s",
            (age, run_id),
        )


def _read(client: TestClient, type_id: str) -> Any:
    r = client.get(f"{URL}/types/{type_id}/profile-fetch")
    assert r.status_code == 200, r.text
    return r.json()["run"]


@pytest.mark.parametrize(
    ("pollers", "age", "status"),
    [
        (False, timedelta(minutes=11), "failed"),
        (True, timedelta(minutes=11), "running"),
        (False, timedelta(minutes=5), "running"),
    ],
)
def test_a_run_no_worker_polls_for_ends_failed(
    client: TestClient, probe: FakeProbe, pollers: bool, age: timedelta, status: str
) -> None:
    """A run still ``running`` 10 minutes after it started, while no worker polls the
    profile-fetch queue, ends ``failed``: none would ever take its workflow. A busy
    worker (pollers present) never fails a queued run, and a young run is left alone."""
    _printer(client)
    type_id = _type(client)
    run = _start(client, type_id).json()["run"]
    _started_ago(client, run["id"], age)
    probe.pollers = pollers
    read = _read(client, type_id)
    assert read["id"] == run["id"]
    assert read["status"] == status
    if status == "failed":
        assert read["error"] == "no profile-fetch worker picked the fetch up"
        assert read["finished_at"] is not None
        # The workflow, if a worker ever takes it, is cancelled too.
        assert probe.cancelled == [run["id"]]
    else:
        assert read["error"] is None
        assert probe.cancelled == []


def test_a_run_past_the_longest_run_ends_failed_with_pollers(
    client: TestClient, probe: FakeProbe
) -> None:
    """The last resort: a run older than the export's queue wait plus both steps'
    schedule-to-close (Decision 13) ends ``failed`` even while a worker polls."""
    _printer(client)
    type_id = _type(client)
    run = _start(client, type_id).json()["run"]
    _started_ago(client, run["id"], LONGEST_RUN + timedelta(minutes=1))
    probe.pollers = True
    read = _read(client, type_id)
    assert read["status"] == "failed"
    # A worker polls, so the text must not say none did (#6625 R-F10).
    assert read["error"] == "the fetch did not end within 25 h; start it again"
    assert probe.cancelled == [run["id"]]


def test_a_queued_run_within_the_execution_timeout_is_left_running(
    client: TestClient, probe: FakeProbe
) -> None:
    """While a worker polls, the API never fails a run before Temporal's own execution
    timeout (plus an hour) could have ended its workflow: a run queued behind many others
    for longer than the export's waits is still served."""
    _printer(client)
    type_id = _type(client)
    run = _start(client, type_id).json()["run"]
    _started_ago(client, run["id"], EXECUTION_TIMEOUT + timedelta(minutes=30))
    probe.pollers = True
    assert _read(client, type_id)["status"] == "running"
    assert probe.cancelled == []


def test_a_cancel_that_fails_still_ends_the_run(
    client: TestClient, probe: FakeProbe, caplog: pytest.LogCaptureFixture
) -> None:
    """The cancel is best effort: a Temporal that refuses it is logged, and the run still
    ends ``failed``."""
    _printer(client)
    type_id = _type(client)
    run = _start(client, type_id).json()["run"]
    _started_ago(client, run["id"], timedelta(minutes=11))
    probe.pollers = False
    probe.cancel_fail = True
    caplog.set_level(logging.WARNING)
    read = _read(client, type_id)
    assert read["status"] == "failed"
    assert any("could not cancel" in r.getMessage() for r in caplog.records)


def test_a_temporal_error_leaves_the_run_running(
    client: TestClient, probe: FakeProbe, caplog: pytest.LogCaptureFixture
) -> None:
    _printer(client)
    type_id = _type(client)
    run = _start(client, type_id).json()["run"]
    _started_ago(client, run["id"], timedelta(minutes=11))
    probe.pollers = False
    probe.fail = True
    caplog.set_level(logging.WARNING)
    assert _read(client, type_id)["status"] == "running"
    assert probe.reads == 1


def test_a_start_that_would_join_an_unserved_run_opens_a_new_one(
    client: TestClient, starter: FakeTemporal, probe: FakeProbe
) -> None:
    """A start that would join a run no worker serves ends that run ``failed`` and opens
    and starts a new one, rather than joining a run that never ends."""
    _printer(client)
    type_id = _type(client)
    first = _start(client, type_id).json()["run"]
    _started_ago(client, first["id"], timedelta(minutes=11))
    probe.pollers = False
    again = _start(client, type_id)
    assert again.status_code == 202, again.text
    assert again.json()["joined"] is False
    assert again.json()["run"]["id"] != first["id"]
    assert [ask.run_id for (_, _, _, ask) in starter.started] == [
        first["id"],
        again.json()["run"]["id"],
    ]
    ended = profile_fetch.fetches(_state(client)).get(UUID(first["id"]))
    assert ended is not None and ended.status == "failed"
    assert ended.error == "no profile-fetch worker picked the fetch up"


def test_a_start_joins_a_run_a_busy_worker_will_take(
    client: TestClient, starter: FakeTemporal, probe: FakeProbe
) -> None:
    _printer(client)
    type_id = _type(client)
    first = _start(client, type_id).json()["run"]
    _started_ago(client, first["id"], timedelta(minutes=11))
    probe.pollers = True
    again = _start(client, type_id)
    assert again.json()["joined"] is True
    assert again.json()["run"]["id"] == first["id"]
    assert len(starter.started) == 1
