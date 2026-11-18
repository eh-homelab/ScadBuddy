"""#6625 Task 4: the public routes that start and read a spool type's 3DFP preset fetch,
and the invoice confirm that starts one for each new type. Temporal is a fake client under
the real `profile_fetch.FetchStarter` (`profile_fetch.starter` patched), as the intake
tests patch `intake.checker`."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.filament import profile_fetch
from scadbuddy.printers.component import PRINTERS
from scadbuddy.profile_fetch.wire import FETCH_WORKFLOW, FetchAsk
from tests.api.test_crud_profile_fetch import _state

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
NOZZLES = ["0.2", "0.4", "0.6", "0.8"]


class FakeTemporal:
    """The workflows the API starts, as `Client.start_workflow` gets them: ``(workflow,
    id, task queue, ask)``, after checking the conflict and reuse policies. ``fail`` makes
    every start raise."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, str, FetchAsk]] = []
        self.fail = False

    async def start_workflow(self, workflow: str, ask: FetchAsk, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("Temporal is down")
        assert kwargs.pop("id_conflict_policy") == WorkflowIDConflictPolicy.USE_EXISTING
        assert kwargs.pop("id_reuse_policy") == WorkflowIDReusePolicy.ALLOW_DUPLICATE
        self.started.append((workflow, kwargs.pop("id"), kwargs.pop("task_queue"), ask))
        assert kwargs == {}


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
        f"profile-fetch:{type_id}",
        "profile-fetch",
    )
    assert ask.run_id == run["id"]
    assert ask.type_id == type_id
    assert ask.tdfp_id == 30173
    assert [t.model_dump() for t in ask.targets] == [{"printer_model": "H2C", "nozzles": NOZZLES}]


def test_a_second_start_joins_the_running_fetch(client: TestClient, starter: FakeTemporal) -> None:
    _printer(client)
    type_id = _type(client)
    first = _start(client, type_id).json()
    again = _start(client, type_id)
    assert again.status_code == 202, again.text
    assert again.json()["joined"] is True
    assert again.json()["run"]["id"] == first["run"]["id"]
    assert len(starter.started) == 1


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
    assert workflow_id == f"profile-fetch:{types['NEW-1']}"
    assert ask.tdfp_id == 30173
    run = client.get(f"{URL}/types/{types['NEW-1']}/profile-fetch").json()["run"]
    assert run["id"] == ask.run_id
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
