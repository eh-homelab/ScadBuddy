"""#3818 tier A Task 6c: a person says which spool is in an untagged tray, on the native
path (plan 2026-10-18 D1): ``tray_spool_links`` only, never Bambuddy's assignment."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.filaments import FilamentOptions, gather_plate_options
from scadbuddy.bambuddy.models import FilamentRequirements
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.spools.component import SPOOLS, Spools
from scadbuddy.spools.models import SpoolSnapshot
from scadbuddy.spools.overlay import printer_overlay, report_status
from tests.printers.conftest import KEK
from tests.support.no_bambuddy import forbid_bambuddy

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000002"
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _printers(client: TestClient) -> Printers:
    return _state(client).components.get(PRINTERS)


def _spools(client: TestClient) -> Spools:
    return _state(client).components.get(SPOOLS)


def _report(white: str = "FFFFFFFF") -> dict[str, Any]:
    return {
        "gcode_state": "IDLE",
        "ams": {
            "ams": [
                {
                    "id": "0",
                    "tray": [
                        {
                            "id": "0",
                            "tray_type": "PLA",
                            "tray_color": "000000FF",
                            "tray_info_idx": "GFA00",
                            "state": 11,
                            "remain": 80,
                            "tray_uuid": UUID_A,
                            "tag_uid": "0102030405060708",
                        },
                        {
                            "id": "1",
                            "tray_type": "PLA",
                            "tray_color": white,
                            "state": 11,
                            "remain": -1,
                            "tray_uuid": "0" * 32,
                            "tag_uid": "0" * 16,
                        },
                        {"id": "2", "tray_type": "", "state": 9},
                    ],
                }
            ]
        },
    }


@pytest.fixture
def lan_printer(client: TestClient) -> Iterator[str]:
    """A LAN-linked row with a fresh report, written straight to the stores."""
    printers = _printers(client)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial=SERIAL, name="Garage"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    printers.reports.put(row.id, _report(), datetime.now(UTC))
    yield row.id


def _bambuddy_record(client: TestClient, record_id: str = "16", **fields: Any) -> None:
    snapshot = SpoolSnapshot.model_validate(
        {
            "record_id": record_id,
            "material": "PLA",
            "colour": "#FFFFFF",
            "brand": "Inland",
            "remaining_g": 515.0,
            **fields,
        }
    )
    client.portal.call(  # type: ignore[union-attr]
        _spools(client).store.replace_records, "bambuddy", [snapshot], datetime.now(UTC)
    )


def _path(printer_id: str, ams_id: int = 0, tray_id: int = 1) -> str:
    return f"/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool"


def _overlay(client: TestClient, printer_id: str) -> Any:
    row = _printers(client).store.require(printer_id)
    report = _printers(client).reports.get(printer_id)
    assert report is not None

    async def read() -> Any:
        return await printer_overlay(_spools(client), row, report, now=datetime.now(UTC))

    return client.portal.call(read)  # type: ignore[union-attr]


def _tray(overlay: Any, ams_id: int, tray_id: int) -> Any:
    return next(t for t in overlay.trays if (t.ams_id, t.tray_id) == (ams_id, tray_id))


def test_tray_spool_links_routes(
    client: TestClient, lan_printer: str, spans: InMemorySpanExporter
) -> None:
    """Link an untagged tray to a Bambuddy record, read it back in the overlay, then
    unlink it; nothing reaches Bambuddy."""
    _bambuddy_record(client)
    with forbid_bambuddy(spans, "https://bambuddy.test"):
        linked = client.put(_path(lan_printer), json={"provider": "bambuddy", "record_id": "16"})
        assert linked.status_code == 200, linked.text
        body = linked.json()
        assert (body["ams_id"], body["tray_id"], body["linked_by"]) == (0, 1, "manual")
        assert body["records"]["bambuddy"]["brand"] == "Inland"
        tray = _tray(_overlay(client, lan_printer), 0, 1)
        assert tray.physical_spool_id == UUID(body["physical_spool_id"])
        assert tray.records["bambuddy"].remaining_g == 515.0

        again = client.put(
            _path(lan_printer), json={"physical_spool_id": body["physical_spool_id"]}
        )
        assert (
            again.status_code == 200
            and again.json()["physical_spool_id"] == (body["physical_spool_id"])
        )

        gone = client.delete(_path(lan_printer))
        assert gone.status_code == 204
        assert _tray(_overlay(client, lan_printer), 0, 1).physical_spool_id is None


def _filaments(client: TestClient, printer_id: str) -> FilamentOptions:
    """The native dialog's filament answer for ``printer_id``: its report's trays with
    the store's overlay over them, as the filaments route reads them (#3818 D1)."""
    row = _printers(client).store.require(printer_id)
    report = _printers(client).reports.get(printer_id)
    assert report is not None

    async def read() -> FilamentOptions:
        now = datetime.now(UTC)
        overlay = await printer_overlay(_spools(client), row, report, now=now)
        (options,) = await gather_plate_options(
            None,
            library_file_id=None,
            plate_ids=[1],
            known=lambda plate: FilamentRequirements(filaments=[]),
            trays_from=report_status(row, report, now=now),
            overlay=overlay,
            now=now,
        )
        return options

    result: FilamentOptions = client.portal.call(read)  # type: ignore[union-attr]
    return result


def test_an_untagged_trays_question_links_through_the_route(
    client: TestClient, lan_printer: str, spans: InMemorySpanExporter
) -> None:
    """#4504: the untagged white tray is asked about with the Bambuddy record no tray
    holds, and the link the dialog sends for it (#4505's ``linkOf``: the physical spool,
    else the first record) is one the route takes; answered, it is not asked again. The
    candidates are the store's, never a Bambuddy read."""
    _bambuddy_record(client)
    with forbid_bambuddy(spans, "https://bambuddy.test"):
        options = _filaments(client, lan_printer)
        (question,) = options.trays
        assert (question.ams_id, question.tray_id) == (0, 1)
        (entry,) = (spool for spool in options.shelf if spool.spool_id in question.candidates)
        assert entry.records["bambuddy"].brand == "Inland"
        provider, record = next(iter(entry.records.items()))
        link: dict[str, str] = (
            {"physical_spool_id": str(entry.physical_spool_id)}
            if entry.physical_spool_id is not None
            else {"provider": provider, "record_id": record.record_id}
        )
        linked = client.put(_path(lan_printer, question.ams_id, question.tray_id), json=link)
        assert linked.status_code == 200, linked.text
        after = _filaments(client, lan_printer)
    assert after.trays == [] and after.shelf == []
    tray = next(spool for spool in after.spools if spool.spool_id == question.spool_id)
    assert tray.records["bambuddy"].record_id == "16"


def test_a_manual_link_overlays_an_untagged_tray_until_its_colour_changes(
    client: TestClient, lan_printer: str
) -> None:
    _bambuddy_record(client)
    assert (
        client.put(_path(lan_printer), json={"provider": "bambuddy", "record_id": "16"}).status_code
        == 200
    )
    assert _tray(_overlay(client, lan_printer), 0, 1).linked_by == "manual"

    _printers(client).reports.put(lan_printer, _report(white="00FF00FF"), datetime.now(UTC))
    tray = _tray(_overlay(client, lan_printer), 0, 1)
    assert (tray.physical_spool_id, tray.records) == (None, {})


def test_an_rfid_tray_resolves_by_its_tag(client: TestClient, lan_printer: str) -> None:
    _bambuddy_record(client, "3", tray_uuid=UUID_A, colour="#000000")
    tray = _tray(_overlay(client, lan_printer), 0, 0)
    assert tray.linked_by == "rfid" and tray.records["bambuddy"].record_id == "3"


def test_the_overlay_names_a_stale_provider(client: TestClient, lan_printer: str) -> None:
    snapshot = SpoolSnapshot(record_id="3", tray_uuid=UUID_A)
    client.portal.call(  # type: ignore[union-attr]
        _spools(client).store.replace_records,
        "bambuddy",
        [snapshot],
        datetime.now(UTC) - timedelta(minutes=31),
    )
    overlay = _overlay(client, lan_printer)
    assert "bambuddy" in overlay.stale and _tray(overlay, 0, 0).records == {}


def test_a_link_names_a_spool_or_a_record(client: TestClient, lan_printer: str) -> None:
    for body in (
        {},
        {"provider": "bambuddy"},
        {"physical_spool_id": "8d6f3c1e-0000-4000-8000-000000000000", "provider": "bambuddy"},
    ):
        assert client.put(_path(lan_printer), json=body).status_code == 422, body


def test_an_unknown_record_or_spool_is_404(client: TestClient, lan_printer: str) -> None:
    missing = client.put(_path(lan_printer), json={"provider": "bambuddy", "record_id": "99"})
    assert missing.status_code == 404 and "99" in missing.json()["detail"]
    spool = client.put(
        _path(lan_printer), json={"physical_spool_id": "8d6f3c1e-0000-4000-8000-000000000000"}
    )
    assert spool.status_code == 404


def test_an_unknown_printer_is_404(client: TestClient) -> None:
    for printer_id in ("8d6f3c1e-0000-4000-8000-000000000001", "not-a-uuid"):
        assert (
            client.put(
                _path(printer_id), json={"provider": "bambuddy", "record_id": "1"}
            ).status_code
            == 404
        )
        assert client.delete(_path(printer_id)).status_code == 404


def test_a_tray_the_report_does_not_show_filled_is_409(
    client: TestClient, lan_printer: str
) -> None:
    _bambuddy_record(client)
    empty = client.put(
        _path(lan_printer, tray_id=2), json={"provider": "bambuddy", "record_id": "16"}
    )
    assert empty.status_code == 409 and "AMS-A slot 3" in empty.json()["detail"]


def test_a_printer_with_no_report_is_409(client: TestClient) -> None:
    printers = _printers(client)
    row = printers.store.create(PrinterCreate(serial="01P00A000000003", name="Attic"))
    answer = client.put(_path(row.id), json={"provider": "bambuddy", "record_id": "1"})
    assert answer.status_code == 409


def test_a_spool_another_tag_names_is_409(client: TestClient, lan_printer: str) -> None:
    _bambuddy_record(client, "3", tray_uuid=UUID_A, colour="#000000")
    _bambuddy_record(client, "16")
    other = client.put(_path(lan_printer), json={"provider": "bambuddy", "record_id": "16"})
    assert other.status_code == 200
    tagged = client.put(
        _path(lan_printer, tray_id=0),
        json={"physical_spool_id": other.json()["physical_spool_id"]},
    )
    assert tagged.status_code == 409


def test_a_printer_deleted_during_the_link_is_404_not_500(
    client: TestClient, lan_printer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rulings (from #4186's review): the racing write's ForeignKeyViolation is the
    printer's 404, not a 500."""
    _bambuddy_record(client)
    store = _spools(client).store
    link = store.link

    async def deleted_first(*args: Any, **kwargs: Any) -> Any:
        _printers(client).store.delete(lan_printer)
        return await link(*args, **kwargs)

    monkeypatch.setattr(store, "link", deleted_first)
    answer = client.put(_path(lan_printer), json={"provider": "bambuddy", "record_id": "16"})
    assert answer.status_code == 404, answer.text


@pytest.mark.parametrize(("ams_id", "tray_id"), [(60, 0), (0, 4), (128, 1), (255, 2), (200, 0)])
def test_a_unit_no_printer_has_is_422(
    client: TestClient, lan_printer: str, ams_id: int, tray_id: int
) -> None:
    """#4336 review M1: AMS 0-15 with trays 0-3, an AMS-HT 128-135 with tray 0, the
    external holders 255 with trays 0-1 (`trays.unit_label`'s spaces)."""
    body = {"provider": "bambuddy", "record_id": "16"}
    assert client.put(_path(lan_printer, ams_id, tray_id), json=body).status_code == 422
    assert client.delete(_path(lan_printer, ams_id, tray_id)).status_code == 422


def test_bambuddys_own_assignment_seeds_the_registry_printers_tray(
    client: TestClient, lan_printer: str
) -> None:
    """#4336 review M4, the #4180 handoff end to end: the registry row's Bambuddy link
    (`BambuddyProvider.printer_key`) is the ``located`` the overlay matches a record's
    own location against."""
    _printers(client).store.link_bambuddy(lan_printer, 5)
    _bambuddy_record(
        client,
        location={"printer": "bambuddy:5", "ams_id": 0, "tray_id": 1, "fingerprint": "PLA|FFFFFF"},
    )
    tray = _tray(_overlay(client, lan_printer), 0, 1)
    assert tray.linked_by == "provider" and tray.records["bambuddy"].record_id == "16"


def test_another_bambuddy_printers_assignment_is_not_this_trays(
    client: TestClient, lan_printer: str
) -> None:
    _printers(client).store.link_bambuddy(lan_printer, 5)
    _bambuddy_record(
        client,
        location={"printer": "bambuddy:6", "ams_id": 0, "tray_id": 1, "fingerprint": "PLA|FFFFFF"},
    )
    assert _tray(_overlay(client, lan_printer), 0, 1).linked_by is None
