"""The printer registry's routes (#3823, plan 2026-10-16-printer-identity-native-path,
Task 3): rows by serial, linking a Bambuddy printer and a LAN connection, and what
Bambuddy has to link. The LAN access code never comes back."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import aiomqtt
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.events import PrinterEvent
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.lan_models import LanReport
from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
CODE = "12345678"
CERT = "cd" * 32


def registry(client: TestClient) -> Printers:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS)


class RecordedWatches:
    """`Printers.watches`, recorded: no route test dials a printer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def ensure(self, printer_id: str) -> None:
        self.calls.append(("ensure", printer_id))

    async def restart(self, printer_id: str) -> None:
        self.calls.append(("restart", printer_id))

    async def stop(self, printer_id: str) -> None:
        self.calls.append(("stop", printer_id))


@pytest.fixture(autouse=True)
def watches(client: TestClient) -> RecordedWatches:
    recorded = RecordedWatches()
    registry(client).watches = recorded  # type: ignore[assignment]
    return recorded


def bambuddy_printer(printer_id: int, serial: str | None, **fields: Any) -> dict[str, Any]:
    return {
        **recording("printers.json")[0],
        "id": printer_id,
        "name": f"P{printer_id}",
        "serial_number": serial,
        **fields,
    }


def bambuddy_printers(*rows: dict[str, Any]) -> respx.Route:
    return respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=list(rows)))


def create(client: TestClient, serial: str = SERIAL, name: str = "H2C") -> dict[str, Any]:
    response = client.post("/api/v1/printers", json={"serial": serial, "name": name})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_create_list_get_patch_delete(client: TestClient) -> None:
    row = create(client)
    assert row["serial"] == SERIAL and row["preferred_path"] == "auto"
    assert row["has_access_code"] is False and row["bambuddy_printer_id"] is None
    assert client.get("/api/v1/printers").json() == [row]
    assert client.get(f"/api/v1/printers/{row['id']}").json() == row
    patched = client.patch(f"/api/v1/printers/{row['id']}", json={"preferred_path": "bambuddy"})
    assert patched.status_code == 200 and patched.json()["preferred_path"] == "bambuddy"
    assert client.delete(f"/api/v1/printers/{row['id']}").status_code == 204
    assert client.get(f"/api/v1/printers/{row['id']}").status_code == 404
    assert client.delete(f"/api/v1/printers/{row['id']}").status_code == 404
    assert client.patch("/api/v1/printers/nope", json={"name": "x"}).status_code == 404


def test_a_duplicate_serial_is_409_naming_the_row(client: TestClient) -> None:
    row = create(client)
    again = client.post("/api/v1/printers", json={"serial": SERIAL, "name": "B"})
    assert again.status_code == 409
    assert row["id"] in again.json()["detail"]


def test_a_malformed_serial_is_422(client: TestClient) -> None:
    assert client.post("/api/v1/printers", json={"serial": "x", "name": "B"}).status_code == 422


@respx.mock
def test_link_bambuddy_and_unlink(client: TestClient) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, SERIAL))
    row = create(client)
    linked = client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    assert linked.status_code == 200, linked.text
    assert linked.json()["bambuddy_printer_id"] == 1
    unlinked = client.delete(f"/api/v1/printers/{row['id']}/bambuddy")
    assert unlinked.status_code == 200 and unlinked.json()["bambuddy_printer_id"] is None


@respx.mock
def test_link_bambuddy_refuses_second_printer_with_same_serial(client: TestClient) -> None:
    """Review Focus 2: a re-added printer reports the serial a linked one has."""
    configure(client)
    bambuddy_printers(bambuddy_printer(1, SERIAL), bambuddy_printer(2, SERIAL))
    row = create(client)
    first = client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    assert first.status_code == 200, first.text
    second = client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 2})
    assert second.status_code == 409
    assert row["id"] in second.json()["detail"]


@respx.mock
def test_link_bambuddy_refuses_an_id_another_row_holds(client: TestClient) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, None))
    a = create(client)
    b = create(client, serial="01P00A000000002", name="B")
    client.put(f"/api/v1/printers/{a['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    taken = client.put(f"/api/v1/printers/{b['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    assert taken.status_code == 409 and a["id"] in taken.json()["detail"]


@respx.mock
def test_link_bambuddy_refuses_serial_mismatch(client: TestClient) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, "01P00A999999999"))
    row = create(client)
    refused = client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})
    assert refused.status_code == 422
    assert "serial" in refused.json()["detail"]


@respx.mock
def test_link_bambuddy_to_a_printer_bambuddy_does_not_have_is_404(client: TestClient) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, SERIAL))
    row = create(client)
    missing = client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 9})
    assert missing.status_code == 404


@respx.mock
def test_discover_marks_linked_and_unlinkable(client: TestClient) -> None:
    """Review Focus 1: a printer whose serial Bambuddy withholds is listed, not linkable."""
    configure(client)
    bambuddy_printers(
        bambuddy_printer(1, SERIAL),
        bambuddy_printer(2, ""),
        bambuddy_printer(3, "01P00A000000003"),
        bambuddy_printer(4, "01P00A000000004", is_active=False),
    )
    row = create(client)
    client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})

    found = client.get("/api/v1/printers/discover")

    assert found.status_code == 200, found.text
    cards = {card["bambuddy_printer_id"]: card for card in found.json()}
    assert set(cards) == {1, 2, 3}
    assert cards[1]["linked_to"] == row["id"] and cards[1]["serial"] == SERIAL
    assert cards[2]["serial"] is None and cards[2]["linkable"] is False
    assert cards[2]["reason"] == (
        "Bambuddy did not report a serial: link it from a printer you created by serial"
    )
    assert cards[3] == {
        "bambuddy_printer_id": 3,
        "name": "P3",
        "model": "H2C",
        "serial": "01P00A000000003",
        "linked_to": None,
        "linkable": True,
        "reason": None,
    }
    assert "ip_address" not in found.text and "10.0.0.1" not in found.text


def test_discover_without_bambuddy_is_empty(client: TestClient) -> None:
    found = client.get("/api/v1/printers/discover")
    assert found.status_code == 200 and found.json() == []


def _probe_ok(calls: list[tuple[str, str, str]]) -> Any:
    async def probe(host: str, serial: str, code: str) -> tuple[LanReport, str]:
        calls.append((host, serial, code))
        return (
            LanReport(serial=serial, received_at=datetime.now(UTC), print={"gcode_state": "IDLE"}),
            CERT,
        )

    return probe


def test_lan_link_never_returns_code(client: TestClient, watches: RecordedWatches) -> None:
    printers = registry(client)
    printers.store.kek = KEK
    calls: list[tuple[str, str, str]] = []
    printers.probe = _probe_ok(calls)
    pid = create(client)["id"]

    response = client.put(
        f"/api/v1/printers/{pid}/lan", json={"host": "10.0.0.9", "access_code": CODE}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_access_code"] is True and "access_code" not in body
    assert body["lan_host"] == "10.0.0.9" and body["lan_cert_sha256"] == CERT
    assert CODE not in response.text
    assert calls == [("10.0.0.9", SERIAL, CODE)]
    assert printers.store.open_access_code(pid) == CODE
    for read in (client.get(f"/api/v1/printers/{pid}"), client.get("/api/v1/printers")):
        assert CODE not in read.text
    unlinked = client.delete(f"/api/v1/printers/{pid}/lan")
    assert unlinked.status_code == 200 and unlinked.json()["has_access_code"] is False
    assert watches.calls == [("restart", pid), ("stop", pid)]


GENERIC = (
    "the printer did not answer at that address, or did not take the access code: check both"
)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionRefusedError(111, "Connection refused"),
        OSError(113, "No route to host"),
        aiomqtt.MqttCodeError(5),
        aiomqtt.MqttError("boom"),
        RuntimeError("anything"),
    ],
)
def test_lan_link_probe_failure_is_502(client: TestClient, failure: BaseException) -> None:
    """Security review: every failure is one message, so the route is no port scanner."""
    printers = registry(client)
    printers.store.kek = KEK

    async def probe(host: str, serial: str, code: str) -> tuple[LanReport, str]:
        raise failure

    printers.probe = probe
    pid = create(client)["id"]

    response = client.put(
        f"/api/v1/printers/{pid}/lan", json={"host": "10.0.0.9", "access_code": CODE}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == GENERIC
    assert CODE not in response.text and "10.0.0.9" not in response.text
    assert client.get(f"/api/v1/printers/{pid}").json()["has_access_code"] is False


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "8.8.8.8", "localhost"]
)
def test_lan_link_refuses_an_address_off_the_lan(client: TestClient, host: str) -> None:
    printers = registry(client)
    printers.store.kek = KEK
    calls: list[tuple[str, str, str]] = []
    printers.probe = _probe_ok(calls)
    pid = create(client)["id"]

    response = client.put(f"/api/v1/printers/{pid}/lan", json={"host": host, "access_code": CODE})

    assert response.status_code == 422, response.text
    assert calls == []


def test_lan_link_refuses_a_host_that_is_not_one(client: TestClient) -> None:
    pid = create(client)["id"]
    response = client.put(
        f"/api/v1/printers/{pid}/lan",
        json={"host": "http://10.0.0.9/", "access_code": CODE},
    )
    assert response.status_code == 422


def test_lan_link_without_kek_is_503(client: TestClient) -> None:
    printers = registry(client)
    printers.store.kek = None
    calls: list[tuple[str, str, str]] = []
    printers.probe = _probe_ok(calls)
    pid = create(client)["id"]

    response = client.put(
        f"/api/v1/printers/{pid}/lan", json={"host": "10.0.0.9", "access_code": CODE}
    )

    assert response.status_code == 503
    assert "SCADBUDDY_SECRET_KEY_FILE" in response.json()["detail"]
    assert calls == []


def test_an_overlong_access_code_is_not_echoed(client: TestClient) -> None:
    pid = create(client)["id"]
    code = "1234567x" * 17
    response = client.put(
        f"/api/v1/printers/{pid}/lan", json={"host": "10.0.0.9", "access_code": code}
    )
    assert response.status_code == 422
    assert "1234567x" not in response.text


def test_every_registry_write_announces_the_printer(client: TestClient) -> None:
    """Task 6: `printer.changed` on `printers` and `printer:<id>` for every write."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    seen: list[PrinterEvent] = []
    publish = state.events.publish

    def record(event: Any) -> None:
        if isinstance(event, PrinterEvent):
            seen.append(event)
        publish(event)

    state.events.publish = record  # type: ignore[method-assign]
    pid = create(client)["id"]
    client.patch(f"/api/v1/printers/{pid}", json={"name": "B"})
    client.delete(f"/api/v1/printers/{pid}")
    assert [(e.kind, e.printer_id) for e in seen] == [("printer.changed", pid)] * 3


# ── #4186 review ─────────────────────────────────────────────────────────────────────


@respx.mock
def test_a_lan_link_on_a_withheld_serial_is_422_before_any_dial(client: TestClient) -> None:
    """I8: the row's serial names the MQTT topics; a synthetic one is refused before the
    access code goes anywhere."""
    configure(client)
    printers = registry(client)
    printers.store.kek = KEK
    calls: list[tuple[str, str, str]] = []
    printers.probe = _probe_ok(calls)
    row = printers.store.ensure_bambuddy(7, serial=None, name="H2C", model=None)

    response = client.put(
        f"/api/v1/printers/{row.id}/lan", json={"host": "10.0.0.9", "access_code": CODE}
    )

    assert response.status_code == 422, response.text
    assert "serial is not known" in response.json()["detail"]
    assert calls == []


@respx.mock
def test_a_bambuddy_linked_printer_is_not_deleted(client: TestClient) -> None:
    """M14: the next /printers/now would make it again under a new id."""
    configure(client)
    bambuddy_printers(bambuddy_printer(1, SERIAL))
    row = create(client)
    client.put(f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1})

    refused = client.delete(f"/api/v1/printers/{row['id']}")

    assert refused.status_code == 409
    assert "unlink" in refused.json()["detail"]
    client.delete(f"/api/v1/printers/{row['id']}/bambuddy")
    assert client.delete(f"/api/v1/printers/{row['id']}").status_code == 204


async def test_a_watch_call_never_hangs_a_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: a lazy Temporal client that cannot connect costs the route a bound, not
    minutes; the link is made and the sweep starts the watch."""
    from scadbuddy.printers import component as component_module

    async def hangs(*args: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(component_module, "restart_watch", hangs)
    monkeypatch.setattr(component_module, "stop_watch", hangs)
    monkeypatch.setattr(component_module, "WATCH_RPC_BOUND", 0.1)
    printers = registry(client)
    watches = component_module.Watches(printers.core)
    async with asyncio.timeout(5):
        await watches.restart("p")
        await watches.stop("p")
