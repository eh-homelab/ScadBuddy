"""#3818 tier A Task 6c: on a native run the filament choices are the printer's loaded
trays, from its LAN report, with the spool providers' cached records laid over them
(plan 2026-10-18 D1). Nothing here reads Bambuddy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.filaments import (
    FilamentPlan,
    every_plate,
    gather_plate_options,
    seed_plan,
)
from scadbuddy.bambuddy.models import (
    AmsTray,
    AmsUnit,
    FilamentRequirement,
    FilamentRequirements,
    NozzleInfo,
    PrinterStatus,
    SlotChoice,
)
from scadbuddy.bambuddy.print_run import resolve_plates
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices, resolve
from scadbuddy.bambuddy.trays import (
    loaded_trays,
    seen_trays,
    spool_loaded,
    tray_spool_id,
    unknown_trays,
)
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import status_from_report
from scadbuddy.slicing.catalogue import native_catalogue
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.spools.models import Overlay, ShelfSpool, SpoolSnapshot, TraySpool
from tests.bambuddy.conftest import recording
from tests.slicing.conftest import PROFILES
from tests.support.no_bambuddy import forbid_bambuddy

API = "http://bambuddy.test"
NOW = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _tray(tray_id: int, material: str = "", colour: str = "", **extra: Any) -> AmsTray:
    fields: dict[str, Any] = {
        "id": tray_id,
        "tray_type": material,
        "tray_color": colour,
        "state": 11 if material else 9,
        **extra,
    }
    return AmsTray.model_validate(fields)


def h2c_status() -> PrinterStatus:
    """AMS-A with three of four trays filled (slot 1 RFID-tagged), an AMS-HT and the
    external holder: five filled trays."""
    return PrinterStatus(
        id=0,
        name="Garage",
        connected=True,
        ams=[
            # Units arrive unsorted (AmsUnit's docstring): the HT first.
            AmsUnit(id=128, is_ams_ht=True, tray=[_tray(0, "PETG", "F95959FF", remain=-1)]),
            AmsUnit(
                id=0,
                tray=[
                    _tray(
                        0,
                        "PLA",
                        "000000FF",
                        tray_info_idx="GFA00",
                        remain=80,
                        tray_uuid=UUID_A,
                        tag_uid="0102030405060708",
                    ),
                    _tray(1, "PLA", "FFFFFFFF", remain=-1),
                    _tray(2),
                    _tray(3, "PETG", "1A1A1AFF", tray_info_idx="GFG00", remain=-1),
                ],
            ),
        ],
        vt_tray=[_tray(254, "PLA", "FF0000FF", remain=-1)],
    )


def h2c_two_nozzles() -> PrinterStatus:
    """:func:`h2c_status` with both extruders named, as an H2C reports them."""
    return h2c_status().model_copy(
        update={
            "nozzles": [
                NozzleInfo(nozzle_type="HS01", nozzle_diameter="0.4"),
                NozzleInfo(nozzle_type="HS01", nozzle_diameter="0.4"),
            ]
        }
    )


def _requirements(*colours: str) -> FilamentRequirements:
    return FilamentRequirements(
        filaments=[
            FilamentRequirement(slot_id=index + 1, type="", color=colour)
            for index, colour in enumerate(colours)
        ]
    )


def _record(record_id: str = "7", **fields: Any) -> SpoolSnapshot:
    return SpoolSnapshot.model_validate(
        {
            "record_id": record_id,
            "material": "PLA",
            "colour": "#000000",
            "brand": "Bambu Lab",
            "remaining_g": 640.0,
            "label_weight_g": 1000,
            **fields,
        }
    )


def _overlay(*trays: TraySpool, stale: dict[str, datetime | None] | None = None) -> Overlay:
    return Overlay(trays=list(trays), stale=stale or {})


async def _gather(status: PrinterStatus, overlay: Overlay | None, *colours: str) -> Any:
    (options,) = await gather_plate_options(
        None,
        library_file_id=None,
        printer_id=None,
        plate_ids=[1],
        known=lambda plate: _requirements(*colours),
        trays_from=status,
        overlay=overlay,
        provider_labels={"bambuddy": "Bambuddy"},
        now=NOW,
    )
    return options


# --- the trays ----------------------------------------------------------------------


def test_every_filled_tray_is_a_choice() -> None:
    options = loaded_trays(h2c_status(), 0, "Garage")
    assert [option.label for option in options] == [
        "AMS-A slot 1",
        "AMS-A slot 2",
        "AMS-A slot 4",
        "HT-A",
        "External",
    ]
    first = options[0]
    assert first.spool_id == tray_spool_id(0, 0) and first.tray_only
    assert (first.material, first.colour, first.slicer_filament) == ("PLA", "#000000", "GFA00")
    assert first.remain_percent == 80 and first.loaded is not None
    # An untagged tray's -1 is "unknown", never a percentage.
    assert options[1].remain_percent is None


def test_two_external_holders_are_named_by_side() -> None:
    status = h2c_two_nozzles().model_copy(
        update={
            "vt_tray": [
                _tray(254, "PLA", "FF0000FF"),
                _tray(255, "PETG", "00FF00FF"),
            ]
        }
    )
    labels = [option.label for option in loaded_trays(status, 0, None)]
    assert labels[-2:] == ["External L", "External R"]


@pytest.mark.parametrize(
    ("state", "loaded"),
    [
        (11, True),  # SPOOL | METADATA | STEADY: the AMS-A's loaded tray
        (9, True),  # SPOOL | STEADY: an AMS-HT's loaded tray (Bambuddy #2594)
        (27, True),  # 0x1B, RFID-flagged: read as empty by the old {10, 11} enum
        (3, True),  # the legacy loaded state (ha-bambulab AMS_TRAY_STATE_LEGACY_MAX)
        (1, False),  # legacy, not settled
        (10, False),  # METADATA | STEADY, no SPOOL: present but not in the feeder
        (13, True),  # SPOOL | MOTION | STEADY: STEADY settles it
        (5, False),  # SPOOL | MOTION: moving, not steady
    ],
)
def test_the_tray_state_is_a_bitfield(state: int, loaded: bool) -> None:
    assert spool_loaded(state) is loaded


def test_an_rfid_flagged_tray_is_filled_on_the_bambuddy_path_too() -> None:
    """Review of #4180 M7: the recorded status's AMS 1 slot 2 reads state 27."""
    status = PrinterStatus.model_validate(recording("printer-status.json"))
    found = {(tray.ams_id, tray.tray_id) for tray in unknown_trays(status, 1, [])}
    assert (1, 1) in found


def test_a_false_presence_bit_vetoes_the_state() -> None:
    """#4336 review I1: Bambuddy's ``exists`` (the firmware's ``tray_exist_bits``,
    "authoritative where it exists", ``services/ams_slot_presence.py`` at ``0d427510``)
    outranks ``state``: the recorded empty AMS 1 slot 3 with a stale ``tray_type``
    echoed back (an AMS-HT does this, Bambuddy #2670) is not a tray."""
    status = PrinterStatus.model_validate(recording("printer-status.json"))
    unit = next(u for u in status.ams if u.id == 1)
    stale = unit.tray[2]
    assert (stale.exists, stale.state) == (False, 9)
    unit.tray[2] = stale.model_copy(update={"tray_type": "PLA", "tray_color": "000000FF"})
    assert (1, 2) not in {(t.ams_id, t.tray_id) for t in unknown_trays(status, 1, [])}
    assert tray_spool_id(1, 2) not in {o.spool_id for o in loaded_trays(status, 1, None)}


def test_state_10_is_hidden_on_the_bambuddy_path() -> None:
    """The bitfield's change there (#4336 M4): 10 is METADATA|STEADY with no SPOOL,
    "spool present but not in the feeder", and a ``True`` presence bit does not
    overrule it, as Bambuddy's own assign path does not (``inventory.py:2270-2283``)."""
    status = PrinterStatus.model_validate(recording("printer-status.json"))
    unit = next(u for u in status.ams if u.id == 1)
    unit.tray[1] = unit.tray[1].model_copy(update={"state": 10, "exists": True})
    assert (1, 1) not in {(t.ams_id, t.tray_id) for t in unknown_trays(status, 1, [])}


def test_one_external_holder_on_a_two_nozzle_printer_is_named_by_its_side() -> None:
    """#4336 review I3: the label is the holder's side, never a count of the list."""
    status = PrinterStatus.model_validate(recording("printer-status.json"))
    status = status.model_copy(update={"vt_tray": [_tray(255, "PETG", "00FF00FF")]})
    (external,) = [o for o in loaded_trays(status, 1, None) if o.loaded and o.loaded.ams_id == 255]
    assert (external.label, external.side) == ("External R", "R")


def test_the_lan_reports_external_holders_are_labelled_by_side() -> None:
    """The LAN report's shape (`status_from_report`): both H2 holders from ``vir_slot``."""
    report = LanReport(
        serial="S",
        received_at=NOW,
        print={
            "device": {
                "nozzle": {
                    "info": [
                        {"id": 0, "diameter": "0.4", "type": "HS01"},
                        {"id": 1, "diameter": "0.4", "type": "HS01"},
                    ]
                }
            },
            "vt_tray": {"id": "255", "tray_type": "PETG", "tray_color": "00FF00FF"},
            "vir_slot": [
                {"id": "254", "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11},
                {"id": "255", "tray_type": "PETG", "tray_color": "00FF00FF", "state": 11},
            ],
        },
    )
    now = datetime.now(UTC)
    row = PrinterRow(id=str(uuid4()), serial="S", name="H2C", created_at=now, updated_at=now)
    status = status_from_report(row, report, now=NOW)
    labels = [(o.label, o.side, o.material) for o in loaded_trays(status, 0, "H2C")]
    assert labels == [("External L", "L", "PLA"), ("External R", "R", "PETG")]


def test_a_single_nozzle_lan_reports_holder_is_external() -> None:
    report = LanReport(
        serial="S",
        received_at=NOW,
        print={"vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "FF0000FF"}},
    )
    now = datetime.now(UTC)
    row = PrinterRow(id=str(uuid4()), serial="S", name="P1S", created_at=now, updated_at=now)
    (option,) = loaded_trays(status_from_report(row, report, now=NOW), 0, "P1S")
    assert option.label == "External"


def test_seen_trays_carry_the_keys_and_the_fingerprint() -> None:
    seen = {(tray.ams_id, tray.tray_id): tray for tray in seen_trays(h2c_status())}
    assert set(seen) == {(0, 0), (0, 1), (0, 3), (128, 0), (255, 0)}
    tagged = seen[(0, 0)]
    assert (tagged.tray_uuid, tagged.tag_uid) == (UUID_A, "0102030405060708")
    assert tagged.fingerprint == "PLA|000000" and tagged.tray_info_idx == "GFA00"
    assert seen[(0, 1)].tray_uuid is None


# --- the overlay -------------------------------------------------------------------


async def test_trays_first_reads_nothing_from_bambuddy(spans: InMemorySpanExporter) -> None:
    with forbid_bambuddy(spans, API):
        options = await _gather(h2c_status(), _overlay(), "#000000", "#FFFFFF")
    assert options.source == "trays"
    assert {choice.spool_id for choice in options.suggested} == {
        tray_spool_id(0, 0),
        tray_spool_id(0, 1),
    }


async def test_an_rfid_tray_shows_its_bambuddy_record() -> None:
    spool = uuid4()
    overlay = _overlay(
        TraySpool(
            ams_id=0,
            tray_id=0,
            physical_spool_id=spool,
            linked_by="rfid",
            records={"bambuddy": _record()},
        )
    )
    options = await _gather(h2c_status(), overlay, "#000000")
    option = next(o for o in options.spools if o.spool_id == tray_spool_id(0, 0))
    assert option.physical_spool_id == spool
    assert option.records["bambuddy"].brand == "Bambu Lab"
    assert (option.brand, option.remaining_g, option.label_weight_g) == ("Bambu Lab", 640.0, 1000)
    assert options.overlay_notes == []


async def test_the_first_providers_record_gives_the_weight() -> None:
    overlay = _overlay(
        TraySpool(
            ams_id=0,
            tray_id=1,
            physical_spool_id=uuid4(),
            linked_by="manual",
            records={
                "native": _record("n1", remaining_g=None, brand=None),
                "bambuddy": _record("7", remaining_g=210.0, brand="Elegoo"),
            },
        )
    )
    options = await _gather(h2c_status(), overlay)
    option = next(o for o in options.spools if o.spool_id == tray_spool_id(0, 1))
    assert (option.remaining_g, option.brand) == (210.0, "Elegoo")


async def test_a_stale_overlay_degrades_with_a_note() -> None:
    """The store leaves a stale provider's records out and names it in ``stale``."""
    overlay = _overlay(
        TraySpool(ams_id=0, tray_id=0),
        stale={"bambuddy": NOW - timedelta(minutes=31)},
    )
    options = await _gather(h2c_status(), overlay, "#000000")
    assert all(not option.records for option in options.spools)
    assert options.overlay_notes == [
        "Bambuddy's spool records are from 13:59 UTC; showing the trays only."
    ]


async def test_a_provider_never_refreshed_says_so() -> None:
    options = await _gather(h2c_status(), _overlay(stale={"bambuddy": None}))
    assert options.overlay_notes == ["No spool records from Bambuddy yet; showing the trays only."]


async def test_an_old_overlay_names_the_day() -> None:
    options = await _gather(
        h2c_status(), _overlay(stale={"bambuddy": NOW - timedelta(days=2, minutes=3)})
    )
    assert options.overlay_notes == [
        "Bambuddy's spool records are from 2026-10-17 14:27 UTC; showing the trays only."
    ]


async def test_no_overlay_is_trays_only_without_a_note() -> None:
    options = await _gather(h2c_status(), None)
    assert options.source == "trays" and options.overlay_notes == []
    assert len(options.spools) == 5


async def test_a_remembered_spool_is_dropped_on_a_native_run() -> None:
    """Review Focus 4: a remembered Bambuddy spool (a positive id) opens with that slot
    on the tray of the closest colour, never a 500 or a Bambuddy read."""
    options = await _gather(h2c_status(), _overlay(), "#FFFFFF")
    plan = seed_plan(options, [SlotChoice(slot_id=1, spool_id=16)])
    assert plan.slots == [SlotChoice(slot_id=1, spool_id=tray_spool_id(0, 1))]


# --- an untagged tray's question (#4504) ---------------------------------------------


def _shelf(record: SpoolSnapshot) -> ShelfSpool:
    """A Bambuddy record linked to no physical spool."""
    return ShelfSpool(records={"bambuddy": record})


def _white_shelf() -> Overlay:
    """Tray (0,1) is untagged white PLA with no spool linked; the shelf has a full and an
    empty white PLA, a black PLA and a white PETG."""
    return Overlay(
        trays=[TraySpool(ams_id=0, tray_id=1)],
        shelf=[
            _shelf(_record("empty", colour="#FFFFFF", remaining_g=0.0)),
            _shelf(_record("black", colour="#000000")),
            _shelf(_record("full", colour="#FAFAFA", remaining_g=800.0, name="Jade White")),
            _shelf(_record("petg", material="PETG", colour="#FFFFFF")),
        ],
    )


async def test_an_untagged_tray_asks_with_the_shelfs_close_records(
    spans: InMemorySpanExporter,
) -> None:
    """Same material, close colour, most filament first and empty last, as the Bambuddy
    path's question; each candidate a shelf entry carrying its records. No Bambuddy read."""
    with forbid_bambuddy(spans, API):
        options = await _gather(h2c_status(), _white_shelf(), "#FFFFFF")
    (question,) = options.trays
    assert (question.ams_id, question.tray_id, question.label) == (0, 1, "AMS-A slot 2")
    assert (question.material, question.colour, question.colour_word) == (
        "PLA",
        "#FFFFFF",
        "white",
    )
    assert question.spool_id == tray_spool_id(0, 1)
    assert question.fingerprint == "PLA|FFFFFF"
    by_id = {entry.spool_id: entry for entry in options.shelf}
    assert [by_id[c].records["bambuddy"].record_id for c in question.candidates] == [
        "full",
        "empty",
    ]
    full = by_id[question.candidates[0]]
    assert full.spool_id > 0 and full.loaded is None and not full.tray_only
    assert (full.material, full.colour, full.color_name, full.remaining_g) == (
        "PLA",
        "#FAFAFA",
        "Jade White",
        800.0,
    )
    # Only what some tray's question offers is sent, and none of it is a print choice.
    assert len(options.shelf) == 2
    assert all(spool.tray_only for spool in options.spools)
    assert {c.spool_id for c in options.suggested} <= {s.spool_id for s in options.spools}


async def test_a_shelf_spool_is_offered_under_its_physical_spool() -> None:
    spool = uuid4()
    overlay = Overlay(
        trays=[TraySpool(ams_id=0, tray_id=1)],
        shelf=[
            ShelfSpool(
                physical_spool_id=spool,
                records={
                    "native": _record("n1", colour="#FFFFFF", remaining_g=None, brand=None),
                    "bambuddy": _record("b1", colour="#FFFFFF", remaining_g=300.0),
                },
            )
        ],
    )
    options = await _gather(h2c_status(), overlay, "#FFFFFF")
    (question,) = options.trays
    (entry,) = options.shelf
    assert question.candidates == [entry.spool_id]
    assert entry.physical_spool_id == spool
    assert list(entry.records) == ["native", "bambuddy"]
    assert (entry.remaining_g, entry.brand) == (300.0, "Bambu Lab")


async def test_a_linked_or_tagged_tray_is_not_asked_about() -> None:
    """Tray (0,1) holds a person's link; (0,0) is RFID-tagged black PLA, and a black PLA
    record on the shelf is close to it: neither is asked about."""
    overlay = Overlay(
        trays=[
            TraySpool(ams_id=0, tray_id=0),
            TraySpool(
                ams_id=0,
                tray_id=1,
                physical_spool_id=uuid4(),
                linked_by="manual",
                records={"bambuddy": _record("w", colour="#FFFFFF")},
            ),
        ],
        shelf=[
            _shelf(_record("black", colour="#000000")),
            _shelf(_record("white", colour="#FFFFFF")),
        ],
    )
    options = await _gather(h2c_status(), overlay, "#FFFFFF")
    assert options.trays == [] and options.shelf == []


async def test_a_tray_with_no_close_record_is_not_asked_about() -> None:
    """On a native run the tray is a choice already; a question with nothing to link
    would only be noise."""
    overlay = Overlay(
        trays=[TraySpool(ams_id=0, tray_id=1)],
        shelf=[_shelf(_record("black", colour="#000000"))],
    )
    options = await _gather(h2c_status(), overlay, "#FFFFFF")
    assert options.trays == [] and options.shelf == []


async def test_without_an_overlay_nothing_is_asked() -> None:
    options = await _gather(h2c_status(), None, "#FFFFFF")
    assert options.trays == [] and options.shelf == []


async def test_every_plate_keeps_the_question_and_suggests_only_trays() -> None:
    plates = await gather_plate_options(
        None,
        library_file_id=None,
        printer_id=None,
        plate_ids=[1, 2],
        known=lambda plate: _requirements("#FFFFFF" if plate == 1 else "#FAFAFA"),
        trays_from=h2c_status(),
        overlay=_white_shelf(),
        now=NOW,
    )
    # Each plate's rows are its own: one plate's edit never reaches another's.
    first, second = plates
    assert first.shelf[0].records is not second.shelf[0].records
    assert first.trays[0].candidates is not second.trays[0].candidates
    merged = every_plate(plates)
    assert [tray.tray_id for tray in merged.trays] == [1]
    assert len(merged.shelf) == 2
    assert {c.spool_id for c in merged.suggested} <= {s.spool_id for s in merged.spools}


# --- the resolver ------------------------------------------------------------------


def _choices() -> PrintChoices:
    return PrintChoices(nozzles=[NozzleChoice(size="0.4")])


@pytest.mark.parametrize("preset", ["GFA00", "GFSA00_99"])
async def test_an_untagged_trays_preset_comes_from_its_record(preset: str) -> None:
    """No ``tray_info_idx``: the provider's preset, a filament id or a cloud setting id."""
    spool = uuid4()
    overlay = _overlay(
        TraySpool(
            ams_id=0,
            tray_id=1,
            physical_spool_id=spool,
            linked_by="manual",
            records={"bambuddy": _record(preset=preset, colour="#FFFFFF")},
        )
    )
    options = await _gather(h2c_status(), overlay, "#FFFFFF")
    plan = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=tray_spool_id(0, 1))])
    resolved = resolve(options, plan, _choices(), native_catalogue(Profiles(PROFILES)), {})
    assert not resolved.errors
    assert resolved.filament_presets[0].id == "Bambu PLA Basic @BBL H2C"


async def test_a_local_record_preset_falls_back_as_any_tray_does() -> None:
    overlay = _overlay(
        TraySpool(
            ams_id=0,
            tray_id=1,
            physical_spool_id=uuid4(),
            linked_by="manual",
            records={"bambuddy": _record(preset="2")},
        )
    )
    options = await _gather(h2c_status(), overlay, "#FFFFFF")
    plan = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=tray_spool_id(0, 1))])
    resolved = resolve(options, plan, _choices(), native_catalogue(Profiles(PROFILES)), {})
    # No preset of Bambuddy's own reaches a native slice: the slot is refused or Generic.
    assert all(ref.source == "standard" for ref in resolved.filament_presets)


async def test_resolve_plates_reads_no_spool_presets_on_a_native_run(
    spans: InMemorySpanExporter,
) -> None:
    """A positive spool id in a native plan is a slot with no choice, and nothing asks
    Bambuddy for its presets: there is no client."""
    options = await _gather(h2c_status(), _overlay(), "#000000", "#FFFFFF")
    plan = FilamentPlan(
        slots=[
            SlotChoice(slot_id=1, spool_id=tray_spool_id(0, 0)),
            SlotChoice(slot_id=2, spool_id=16),
        ]
    )
    with forbid_bambuddy(spans, API):
        (resolved,), errors = await resolve_plates(
            None, [1], [options], plan, _choices(), native_catalogue(Profiles(PROFILES))
        )
    assert resolved.filament_presets[0].id == "Bambu PLA Basic @BBL H2C"
    assert errors == ["Slot 2 has no spool chosen."]


# --- the Bambuddy path ---------------------------------------------------------------


@respx.mock
async def test_the_inventory_path_is_unchanged() -> None:
    """Without ``trays_from``: today's reads, in today's order."""
    order: list[str] = []

    def answer(body: Any) -> Any:
        def side_effect(request: httpx.Request) -> httpx.Response:
            order.append(request.url.path)
            return httpx.Response(200, json=body)

        return side_effect

    respx.get(f"{API}/api/v1/inventory/spools").mock(side_effect=answer([]))
    respx.get(f"{API}/api/v1/inventory/assignments").mock(side_effect=answer([]))
    respx.get(f"{API}/api/v1/printers/1").mock(
        side_effect=answer(recording("printers.json")[0] | {"id": 1})
    )
    respx.get(f"{API}/api/v1/printers/1/inventory-remain").mock(
        side_effect=answer({"slot_materials": []})
    )
    async with BambuddyClient(BambuddyConfig(base_url=API, api_key="k")) as client:
        (options,) = await gather_plate_options(
            client,
            library_file_id=None,
            printer_id=1,
            plate_ids=[1],
            known=lambda plate: _requirements("#000000"),
        )
    assert options.source == "inventory" and options.overlay_notes == []
    assert order == [
        "/api/v1/inventory/spools",
        "/api/v1/inventory/assignments",
        "/api/v1/printers/1",
        "/api/v1/printers/1/inventory-remain",
    ]


def test_the_rfid_keys_stay_in_the_backend() -> None:
    """#4336 review M6: read by the overlay, never served in a status."""
    tray = AmsTray(id=0, tray_type="PLA", tray_uuid=UUID_A, tag_uid="0102030405060708")
    assert tray.tray_uuid == UUID_A
    assert "tray_uuid" not in tray.model_dump() and "tag_uid" not in tray.model_dump(mode="json")
    assert "tray_uuid" not in AmsTray.model_json_schema(mode="serialization")["properties"]
