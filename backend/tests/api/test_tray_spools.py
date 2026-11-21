"""#3818 tier A Task 6c, #6262 Task 4: a person says which SB spool is in an untagged
tray, on the native path: the spool's tray place (`filament/inventory.py`), never
Bambuddy's assignment."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.filaments import FilamentOptions, gather_plate_options
from scadbuddy.bambuddy.models import FilamentRequirements
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.filament.component import FILAMENT, Filament
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import report_status
from tests.printers.conftest import KEK
from tests.support.no_bambuddy import forbid_bambuddy

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000002"
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


#: A well-formed body naming no spool, for the refusals that come first.
_ANY = {"spool_id": "8d6f3c1e-0000-4000-8000-0000000000ff"}


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _printers(client: TestClient) -> Printers:
    return _state(client).components.get(PRINTERS)


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


def _path(printer_id: str, ams_id: int = 0, tray_id: int = 1) -> str:
    return f"/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool"


def _filament(client: TestClient) -> Filament:
    return _state(client).components.get(FILAMENT)


def _sb_spool(client: TestClient) -> UUID:
    filament = _filament(client)
    material = next(
        r.id
        for r in filament.catalog.list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return filament.spools.create(SpoolCreate(material_id=material), actor="ann").id


def _held(client: TestClient, printer_id: str, tray_id: int = 1) -> UUID | None:
    """The SB spool the printer's latest report holds in AMS 0 ``tray_id``."""
    row = _printers(client).store.require(printer_id)
    report = _printers(client).reports.get(printer_id)
    assert report is not None
    seen = seen_trays(report_status(row, report, now=datetime.now(UTC)))
    view = _filament(client).inventory.trays(UUID(printer_id), seen)
    holder = next(t for t in view.trays if (t.ams_id, t.tray_id) == (0, tray_id))
    return None if holder.spool is None else holder.spool.id


def _filaments(client: TestClient, printer_id: str) -> FilamentOptions:
    """The native dialog's filament answer for ``printer_id``: its report's trays with
    the SB spools each holds, as the filaments route reads them (#6262 Task 5)."""
    row = _printers(client).store.require(printer_id)
    report = _printers(client).reports.get(printer_id)
    assert report is not None
    status = report_status(row, report, now=datetime.now(UTC))
    held = _filament(client).inventory.trays(UUID(printer_id), seen_trays(status))

    async def read() -> FilamentOptions:
        (options,) = await gather_plate_options(
            None,
            library_file_id=None,
            plate_ids=[1],
            known=lambda plate: FilamentRequirements(filaments=[]),
            trays_from=status,
            held=held,
        )
        return options

    options: FilamentOptions = client.portal.call(read)  # type: ignore[union-attr]
    return options


def _white_spool(client: TestClient, grams: float) -> UUID:
    filament = _filament(client)
    material = next(
        r.id
        for r in filament.catalog.list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    body = SpoolCreate(material_id=material, rgba="#FFFFFF", grams_left=grams)
    return filament.spools.create(body, actor="ann").id


def test_an_untagged_trays_question_links_through_the_route(
    client: TestClient, lan_printer: str, spans: InMemorySpanExporter
) -> None:
    """#4504, #6262 Task 5: each untagged white tray is asked about with the white SB
    spools no tray holds, under their numbers. The answer the dialog sends (`linkOf`: the
    candidate's ``sb_spool_id``) is one the route takes; answered, the tray holds the spool
    and is not asked about again, and the other tray is offered only the other spool."""
    full, half = _white_spool(client, 515.0), _white_spool(client, 200.0)
    # A second untagged white tray, asked about with the same spools until one is linked.
    report = _report()
    report["ams"]["ams"][0]["tray"][2] = {**report["ams"]["ams"][0]["tray"][1], "id": "2"}
    _printers(client).reports.put(lan_printer, report, datetime.now(UTC))
    with forbid_bambuddy(spans, "https://bambuddy.test"):
        options = _filaments(client, lan_printer)
        question, second = options.trays
        assert (question.ams_id, question.tray_id, second.tray_id) == (0, 1, 2)
        assert second.candidates == question.candidates
        by_number = {entry.spool_id: entry for entry in options.shelf}
        assert [by_number[c].sb_spool_id for c in question.candidates] == [full, half]
        assert all(by_number[c].sb_number == c for c in question.candidates)
        entry = by_number[question.candidates[0]]
        linked = client.put(
            _path(lan_printer, question.ams_id, question.tray_id),
            json={"spool_id": str(entry.sb_spool_id)},
        )
        assert linked.status_code == 200, linked.text
        after = _filaments(client, lan_printer)
    (left,) = after.trays
    assert (left.ams_id, left.tray_id) == (0, 2)
    named = {spool.spool_id: spool for spool in after.shelf}
    assert [named[c].sb_spool_id for c in left.candidates] == [half]
    tray = next(spool for spool in after.spools if spool.spool_id == question.spool_id)
    assert (tray.sb_spool_id, tray.remaining_g) == (full, 515.0)


def test_tray_spool_links_routes(
    client: TestClient, lan_printer: str, spans: InMemorySpanExporter
) -> None:
    """Link an SB spool to an untagged tray, read it back as the tray's, then unlink it;
    nothing reaches Bambuddy."""
    spool = _sb_spool(client)
    with forbid_bambuddy(spans, "https://bambuddy.test"):
        linked = client.put(_path(lan_printer), json={"spool_id": str(spool)})
        assert linked.status_code == 200, linked.text
        body = linked.json()
        assert body["id"] == str(spool) and body["location_id"] is not None
        assert _held(client, lan_printer) == spool

        gone = client.delete(_path(lan_printer))
        assert gone.status_code == 204
        assert _held(client, lan_printer) is None
        after = _filament(client).spools.get(spool)
        assert after is not None and after.location_id is None


def test_a_link_held_until_the_trays_colour_changes(client: TestClient, lan_printer: str) -> None:
    spool = _sb_spool(client)
    assert client.put(_path(lan_printer), json={"spool_id": str(spool)}).status_code == 200
    assert _held(client, lan_printer) == spool
    _printers(client).reports.put(lan_printer, _report(white="00FF00FF"), datetime.now(UTC))
    assert _held(client, lan_printer) is None


def test_a_reported_trays_alpha_is_part_of_its_fingerprint(
    client: TestClient, lan_printer: str
) -> None:
    """Spec §5.1: the report's ``tray_color`` alpha reaches the fingerprint, so an opaque
    link does not hold a translucent refill of the same hue."""
    spool = _sb_spool(client)
    assert client.put(_path(lan_printer), json={"spool_id": str(spool)}).status_code == 200
    assert _held(client, lan_printer) == spool
    _printers(client).reports.put(lan_printer, _report(white="FFFFFF80"), datetime.now(UTC))
    row = _printers(client).store.require(lan_printer)
    report = _printers(client).reports.get(lan_printer)
    assert report is not None
    tray = seen_trays(report_status(row, report, now=datetime.now(UTC)))[1]
    assert (tray.colour, tray.rgba) == ("#FFFFFF", "#FFFFFF80")
    assert _held(client, lan_printer) is None


def test_a_link_names_an_sb_spool(client: TestClient, lan_printer: str) -> None:
    for body in (
        {},
        {"spool_id": "16"},
        {"provider": "bambuddy", "record_id": "16"},
        {"physical_spool_id": "8d6f3c1e-0000-4000-8000-000000000000"},
    ):
        assert client.put(_path(lan_printer), json=body).status_code == 422, body


def test_an_unknown_spool_is_404(client: TestClient, lan_printer: str) -> None:
    missing = "8d6f3c1e-0000-4000-8000-000000000000"
    answer = client.put(_path(lan_printer), json={"spool_id": missing})
    assert answer.status_code == 404 and missing in answer.json()["detail"]


def test_a_virtual_printer_has_no_trays(client: TestClient, lan_printer: str) -> None:
    """#4747: a Bambuddy virtual printer is not hardware, so no spool goes in its trays."""
    with _printers(client).store.pool.connection() as conn:
        conn.execute(
            "UPDATE printers SET kind = 'bambuddy_virtual', bambuddy_vp_id = 1 WHERE id = %s",
            (lan_printer,),
        )
    response = client.put(_path(lan_printer), json=_ANY)
    assert response.status_code == 409 and "virtual" in response.json()["detail"]


def test_an_unknown_printer_is_404(client: TestClient) -> None:
    for printer_id in ("8d6f3c1e-0000-4000-8000-000000000001", "not-a-uuid"):
        assert client.put(_path(printer_id), json=_ANY).status_code == 404
        assert client.delete(_path(printer_id)).status_code == 404


def test_a_tray_the_report_does_not_show_filled_is_409(
    client: TestClient, lan_printer: str
) -> None:
    empty = client.put(_path(lan_printer, tray_id=2), json={"spool_id": str(_sb_spool(client))})
    assert empty.status_code == 409 and "AMS-A slot 3" in empty.json()["detail"]


def test_a_printer_with_no_report_is_409(client: TestClient) -> None:
    printers = _printers(client)
    row = printers.store.create(PrinterCreate(serial="01P00A000000003", name="Attic"))
    answer = client.put(_path(row.id), json=_ANY)
    assert answer.status_code == 409


def test_a_tag_bound_elsewhere_refuses_the_link(client: TestClient, lan_printer: str) -> None:
    """Tray 0 reports tag ``UUID_A``, which is on spool B: A cannot be placed there."""
    a, b = _sb_spool(client), _sb_spool(client)
    _filament(client).spools.bind_tag(b, tray_uuid=UUID_A, kind="bambu_mifare", actor="ann")
    tagged = client.put(_path(lan_printer, tray_id=0), json={"spool_id": str(a)})
    assert tagged.status_code == 409, tagged.text
    assert "tag" in tagged.json()["detail"]


def test_a_tray_whose_keys_name_two_tags_is_409(client: TestClient, lan_printer: str) -> None:
    """Tray 0's ``tray_uuid`` and ``tag_uid`` name two different free tags."""
    a, b, c = _sb_spool(client), _sb_spool(client), _sb_spool(client)
    spools = _filament(client).spools
    spools.bind_tag(b, tray_uuid=UUID_A, kind="bambu_mifare", actor="ann")
    spools.bind_tag(c, tag_uid="0102030405060708", kind="other", actor="ann")
    for tag in spools.tags():
        spools.unbind_tag(tag.id, actor="ann")
    answer = client.put(_path(lan_printer, tray_id=0), json={"spool_id": str(a)})
    assert answer.status_code == 409, answer.text
    after = spools.get(a)
    assert after is not None and after.location_id is None


def test_a_printer_deleted_during_the_link_is_404_not_500(
    client: TestClient, lan_printer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rulings (from #4186's review): a printer gone between the read and the write is
    the printer's 404, not a 500."""
    spool = _sb_spool(client)
    inventory = _filament(client).inventory
    link = inventory.link

    def deleted_first(*args: Any, **kwargs: Any) -> Any:
        _printers(client).store.delete(lan_printer)
        return link(*args, **kwargs)

    monkeypatch.setattr(inventory, "link", deleted_first)
    answer = client.put(_path(lan_printer), json={"spool_id": str(spool)})
    assert answer.status_code == 404, answer.text


@pytest.mark.parametrize(("ams_id", "tray_id"), [(60, 0), (0, 4), (128, 1), (255, 2), (200, 0)])
def test_a_unit_no_printer_has_is_422(
    client: TestClient, lan_printer: str, ams_id: int, tray_id: int
) -> None:
    """#4336 review M1: AMS 0-15 with trays 0-3, an AMS-HT 128-135 with tray 0, the
    external holders 255 with trays 0-1 (`trays.unit_label`'s spaces)."""
    assert client.put(_path(lan_printer, ams_id, tray_id), json=_ANY).status_code == 422
    assert client.delete(_path(lan_printer, ams_id, tray_id)).status_code == 422
