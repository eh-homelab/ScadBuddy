"""A native rack print's ``nozzle_mapping`` (hot-path plan 2026-10-18 Task 8): the wire,
the sliced 3MF's rack plan, and the pick from the printer's LAN report, checked against
what was measured on the live H2C (``fixtures/rack-project-file.json``)."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.printers import nozzle_mapping as nozzle_mapping_module
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.nozzle_mapping import (
    FIXED_NOZZLE_ID,
    RACK_WIRE_SLOTS,
    SlicedRack,
    choose_nozzles,
    nozzle_mapping,
    rack_nozzle_id,
    read_rack_plan,
)
from scadbuddy.printers.status import status_from_report
from scadbuddy.rack.rank import eligible, manual_for, rack_positions
from tests.printers.test_status import row
from tests.support.sliced3mf import with_corrupt_member

FIXTURES = Path(__file__).parent / "fixtures"
SLICE_INFO = (FIXTURES / "rack-slice-info-h2c.config").read_text(encoding="utf-8")
MEASURED = json.loads((FIXTURES / "rack-project-file.json").read_text(encoding="utf-8"))
#: The H2C preset's two keys the rack plan reads (``bambuddy/extruders.py``).
H2C_SETTINGS: dict[str, Any] = {
    "extruder_max_nozzle_count": ["1", "6"],
    "physical_extruder_map": ["1", "0"],
}


def pad(values: list[int]) -> list[int]:
    return values + [-1] * (RACK_WIRE_SLOTS - len(values))


def sliced(slice_info: str = SLICE_INFO, **settings: Any) -> io.BytesIO:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "Metadata/project_settings.config", json.dumps({**H2C_SETTINGS, **settings})
        )
        archive.writestr("Metadata/slice_info.config", slice_info)
    out.seek(0)
    return out


def idle_status() -> PrinterStatus:
    data = json.loads((FIXTURES / "lan-report-h2c-idle.json").read_text(encoding="utf-8"))
    data.pop("firmware")
    report = LanReport.model_validate(data)
    return status_from_report(row(), report, now=report.received_at)


# The wire.


def test_rack_nozzle_id_is_15_plus_the_position() -> None:
    assert [rack_nozzle_id(p) for p in range(1, 7)] == [16, 17, 18, 19, 20, 21]
    assert rack_nozzle_id(0) is None and rack_nozzle_id(7) is None
    assert FIXED_NOZZLE_ID == 1 and RACK_WIRE_SLOTS == 32


def test_one_physical_nozzle_per_slot() -> None:
    assert nozzle_mapping([2, 0, 1], {0}, {1: 1, 2: 3}) == pad([18, 1, 16])


def test_an_unprinted_slot_is_minus_one() -> None:
    assert nozzle_mapping([1, -1, 0], {0}, {1: 2}) == pad([17, -1, 1])


def test_no_rack_group_sends_no_mapping() -> None:
    assert nozzle_mapping([0, 0], {0}, {}) is None


def test_a_pick_outside_the_rack_sends_no_mapping(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert nozzle_mapping([1, 0], {0}, {1: 7}) is None
    assert "rack position" in caplog.text


def test_a_rack_group_without_a_pick_sends_no_mapping(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert nozzle_mapping([1, 2, 0], {0}, {1: 1}) is None
    assert "no rack position" in caplog.text


def test_more_slots_than_the_wire_sends_no_mapping() -> None:
    assert nozzle_mapping([1] * (RACK_WIRE_SLOTS + 1), set(), {1: 1}) is None


def test_bambuddys_dispatches_are_the_wire_this_writes() -> None:
    """Each completed dispatch is 32 entries of 1, 16-21 or -1, as this writes them."""
    for dispatch in MEASURED["dispatches"]:
        wire = dispatch["nozzle_mapping"]
        assert len(wire) == RACK_WIRE_SLOTS
        assert set(wire) <= {-1, FIXED_NOZZLE_ID, *range(16, 22)}
        # Rebuilt from the groups it implies: one per distinct nozzle.
        groups = {nozzle: index for index, nozzle in enumerate(dict.fromkeys(wire))}
        slot_groups = [groups[n] if n != -1 else -1 for n in wire]
        fixed = {groups[FIXED_NOZZLE_ID]} if FIXED_NOZZLE_ID in groups else set()
        picks = {g: n - 15 for n, g in groups.items() if n >= 16}
        assert nozzle_mapping(slot_groups, fixed, picks) == wire


# The sliced 3MF's rack plan.


def test_the_measured_plates_rack_plan() -> None:
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    assert plan.slot_groups == MEASURED["plate"]["slot_groups"]
    assert plan.fixed_groups == set(MEASURED["plate"]["fixed_groups"])
    shapes = {
        f.group_id: (f.group.nozzle_diameter, f.group.volume_type)
        for f in plan.filaments
        if f.group is not None and f.group.on_rack
    }
    assert shapes == {
        1: ("0.40", "Standard"),
        2: ("0.40", "Standard"),
        3: ("0.40", "High Flow"),
        4: ("0.40", "High Flow"),
    }


def test_a_plan_crosses_a_workflow_history() -> None:
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    assert SlicedRack.model_validate_json(plan.model_dump_json()) == plan


def test_the_only_plate_is_read_whatever_its_number() -> None:
    assert read_rack_plan(sliced(), 3) is not None


def test_a_printer_without_a_rack_has_no_plan() -> None:
    assert (
        read_rack_plan(
            sliced(extruder_max_nozzle_count=["1", "1"], physical_extruder_map=["1", "0"]), 1
        )
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        # A carriage map this translation does not model.
        lambda info, settings: (info, {**settings, "physical_extruder_map": ["0", "1"]}),
        # A group the nozzle table does not place.
        lambda info, settings: (info.replace('<nozzle id="4" extruder_id="2"', "<x"), settings),
        # Two filaments of one group wanting different hotends.
        lambda info, settings: (
            info.replace(
                'group_id="1" nozzle_diameter="0.40" volume_type="Standard" used_for_object'
                '="true" used_for_support="false" total_load_time="135.00"',
                'group_id="1" nozzle_diameter="0.40" volume_type="High Flow" used_for_object'
                '="true" used_for_support="false" total_load_time="135.00"',
            ),
            settings,
        ),
        # A filament with no group.
        lambda info, settings: (info.replace(' group_id="0"', ""), settings),
        # The table and the filament disagree about the hotend.
        lambda info, settings: (
            info.replace(
                '<nozzle id="3" extruder_id="2" nozzle_diameter="0.4" volume_type="High Flow"/>',
                '<nozzle id="3" extruder_id="2" nozzle_diameter="0.4" volume_type="Standard"/>',
            ),
            settings,
        ),
    ],
)
def test_a_plan_that_cannot_be_resolved_is_none(
    change: Any, caplog: pytest.LogCaptureFixture
) -> None:
    info, settings = change(SLICE_INFO, H2C_SETTINGS)
    with caplog.at_level(logging.WARNING):
        assert read_rack_plan(sliced(info, **settings), 1) is None
    assert caplog.records


def test_a_corrupt_member_has_no_plan(caplog: pytest.LogCaptureFixture) -> None:
    """A deflated slice_info.config whose stream is damaged: the read raises inside
    zipfile (``zlib.error`` or a bad CRC), never out of ``read_rack_plan``."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps(H2C_SETTINGS))
        archive.writestr("Metadata/slice_info.config", SLICE_INFO)
    data = bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("Metadata/slice_info.config")
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    for at in range(start + 8, start + 40):
        data[at] ^= 0xFF
    with caplog.at_level(logging.WARNING):
        assert read_rack_plan(io.BytesIO(bytes(data)), 1) is None
    assert "unreadable" in caplog.text


def test_an_unsupported_compression_has_no_plan() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps(H2C_SETTINGS))
        archive.writestr("Metadata/slice_info.config", SLICE_INFO)
    patched = bytearray(out.getvalue())
    # The central directory's compression method (offset 10 of the entry) as 99, which
    # zipfile refuses with NotImplementedError.
    entry = patched.rfind(b"PK\x01\x02")
    assert patched[entry + 46 : entry + 46 + 26] == b"Metadata/slice_info.config"
    patched[entry + 10 : entry + 12] = (99).to_bytes(2, "little")
    assert read_rack_plan(io.BytesIO(bytes(patched)), 1) is None


def test_let_the_printer_pick_is_said_as_declined() -> None:
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    chosen = choose_nozzles(
        plan, idle_status(), algorithm="bambuddy", manual_position=None, usage={}, spools={}
    )
    assert chosen.declined
    least = choose_nozzles(
        plan, idle_status(), algorithm="least_used", manual_position=None, usage={}, spools={}
    )
    assert not least.declined


def test_a_declined_pick_says_the_hand_picked_position_was_not_sent() -> None:
    """#4545: with several rack groups, "Let Bambuddy pick" sends nothing, the position
    picked by hand included, so ``manual_for``'s "group 1 got position 6" is not said."""
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    chosen = choose_nozzles(
        plan, idle_status(), algorithm="bambuddy", manual_position=6, usage={}, spools={}
    )
    assert chosen.declined and chosen.mapping is None
    manual = [w.message for w in chosen.warnings if w.kind == "rack-manual-partial"]
    assert manual == [
        "Rack position 6 was not sent: the slice put the rack side in 4 nozzle groups, and"
        " over the LAN the printer picks for all of them or none."
    ]


def test_a_declined_pick_says_an_unfitting_position_once() -> None:
    """#4555: position 1 (a 0.2) does not fit group 1 (0.4 Standard). The run says it
    was not sent, and not a second time that it was not used."""
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    chosen = choose_nozzles(
        plan, idle_status(), algorithm="bambuddy", manual_position=1, usage={}, spools={}
    )
    assert chosen.declined
    manual = [w.message for w in chosen.warnings if w.kind == "rack-manual-partial"]
    assert manual == [
        "Rack position 1 was not sent: the slice put the rack side in 4 nozzle groups, and"
        " over the LAN the printer picks for all of them or none."
    ]
    # Under any kind, so a renamed kind cannot let the duplicate back in (#4575).
    assert not any(
        "does not fit" in w.message or "so it was not used" in w.message for w in chosen.warnings
    )


def test_a_declined_pick_says_the_printer_picks_an_abrasives_nozzle() -> None:
    """#4555: over the LAN it is the printer, not Bambuddy, that picks the hotend."""
    info = SLICE_INFO.replace('type="PLA" color="#8344B080"', 'type="PLA-CF" color="#8344B080"')
    plan = read_rack_plan(sliced(info), 1)
    assert plan is not None
    chosen = choose_nozzles(
        plan, idle_status(), algorithm="bambuddy", manual_position=None, usage={}, spools={}
    )
    unsafe = [w.message for w in chosen.warnings if w.kind == "rack-unsafe-material"]
    assert unsafe == [
        "The printer picks the nozzle and may use a non-hardened one for PLA-CF, PLA."
    ]


def test_the_hand_picked_positions_notes_name_the_printer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4586 review: over the LAN, `choose_nozzles` hands ``manual_for`` the printer as
    the picker, never Bambuddy (#4630, #4650 reviews)."""
    asked: list[dict[str, Any]] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs)
        return manual_for(*args, **kwargs)

    monkeypatch.setattr(nozzle_mapping_module, "manual_for", recorded)
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    choose_nozzles(
        plan, idle_status(), algorithm="bambuddy", manual_position=6, usage={}, spools={}
    )
    # The wording each picker gives is `tests/rack/test_rank.py`'s (#4600 review).
    assert asked == [{"picker": "the printer"}]


def test_a_file_with_no_slice_info_has_no_plan() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps(H2C_SETTINGS))
    assert read_rack_plan(out, 1) is None
    assert read_rack_plan(io.BytesIO(b"not a zip"), 1) is None


# The pick, end to end: item 370's plate against the rack the printer reported that day.


def _valid(wire: list[int], plan: SlicedRack, status: PrinterStatus) -> None:
    """Every slot on a nozzle of its group's diameter and flow, one group per rack
    hotend, the fixed group on the fixed hotend."""
    positions = rack_positions(status.nozzle_rack)
    shape = {f.group_id: f.group for f in plan.filaments if f.group_id is not None and f.group}
    nozzle_of: dict[int, int] = {}
    for slot, group_id in enumerate(plan.slot_groups):
        nozzle = wire[slot]
        assert nozzle_of.setdefault(group_id, nozzle) == nozzle
        group = shape[group_id]
        if not group.on_rack:
            assert nozzle == FIXED_NOZZLE_ID
            continue
        hotend = positions[nozzle - 15]
        assert eligible(hotend, group.nozzle_diameter, group.volume_type)
    rack = [n for n in nozzle_of.values() if n != FIXED_NOZZLE_ID]
    assert len(rack) == len(set(rack))
    assert wire[len(plan.slot_groups) :] == [-1] * (RACK_WIRE_SLOTS - len(plan.slot_groups))


def test_the_measured_answer_is_valid() -> None:
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    _valid(MEASURED["plate"]["nozzle_mapping"], plan, idle_status())


@pytest.mark.parametrize("algorithm", ["least_used", "oldest_first", "newest_first"])
def test_the_pick_is_bambuddys_on_the_measured_plate(algorithm: Any) -> None:
    """Deterministic here: each group's colour is loaded on one eligible hotend but
    group 4's, which takes the one left, so every ranking gives Bambuddy's order."""
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    status = idle_status()
    chosen = choose_nozzles(
        plan, status, algorithm=algorithm, manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is not None
    _valid(chosen.mapping, plan, status)
    assert chosen.mapping == MEASURED["plate"]["nozzle_mapping"]
    assert {pick.group_id: pick.position for pick in chosen.picks} == {1: 2, 2: 6, 3: 5, 4: 3}


def test_the_carriages_hotend_is_the_missing_position() -> None:
    """The report omits id 19 while it is on the carriage (id 0, ``src_id`` 19): that
    hotend, an HS01 0.6, is position 4, so a 0.6 Standard group gets 19."""
    status = idle_status()
    carriage = rack_positions(status.nozzle_rack)[4]
    assert (carriage.nozzle_type, carriage.nozzle_diameter) == ("HS01", "0.6")
    info = SLICE_INFO.replace(
        'group_id="2" nozzle_diameter="0.40" volume_type="Standard"',
        'group_id="2" nozzle_diameter="0.60" volume_type="Standard"',
    ).replace(
        '<nozzle id="2" extruder_id="2" nozzle_diameter="0.4"',
        '<nozzle id="2" extruder_id="2" nozzle_diameter="0.6"',
    )
    plan = read_rack_plan(sliced(info), 1)
    assert plan is not None
    chosen = choose_nozzles(
        plan, status, algorithm="least_used", manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is not None and chosen.mapping[0] == 19


def test_no_eligible_hotend_sends_no_mapping() -> None:
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    status = idle_status()
    # Only the 0.2 left on the rack.
    status.nozzle_rack = [slot for slot in status.nozzle_rack if slot.id in (1, 16)]
    chosen = choose_nozzles(
        plan, status, algorithm="least_used", manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is None
    # #4575: over the LAN the printer picks what is left, not Bambuddy.
    left = [w.message for w in chosen.warnings if w.kind == "rack-left-to-bambuddy"]
    assert left and all(m.startswith("Rack pick left to the printer: ") for m in left)
    assert not any("Bambuddy" in w.message for w in chosen.warnings)


def test_let_the_printer_pick_sends_no_mapping() -> None:
    """The "bambuddy" ranking picks nothing itself: the firmware picks, as before."""
    plan = read_rack_plan(sliced(), 1)
    assert plan is not None
    chosen = choose_nozzles(
        plan, idle_status(), algorithm="bambuddy", manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is None and chosen.picks == []


def test_a_plate_on_the_fixed_hotend_only_sends_no_mapping() -> None:
    info = "\n".join(
        line.replace('extruder_id="2"', 'extruder_id="1"') for line in SLICE_INFO.splitlines()
    )
    plan = read_rack_plan(sliced(info), 1)
    assert plan is not None and plan.fixed_groups == {0, 1, 2, 3, 4}
    chosen = choose_nozzles(
        plan, idle_status(), algorithm="least_used", manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is None


#: Real Bambu Studio CLI slices for the H2C (#4711, `tests/slicing/test_slice_info.py`).
NATIVE = Path(__file__).parents[1] / "fixtures" / "native_slices"


def test_a_one_filament_plates_plan_is_on_the_slot_it_prints() -> None:
    """#4712: the CLI names a one-filament plate's filament ``id="1"`` whatever it prints;
    plate 3 prints filament 3 from the rack (group 1), so slot 3 is on the rack and slot
    1 is not printed. Keyed on ``id="1"``, the wire put the rack nozzle on slot 1."""
    with (NATIVE / "plate3-one-filament.gcode.3mf").open("rb") as file:
        plan = read_rack_plan(file, 3)
    assert plan is not None
    assert plan.slot_groups == [-1, -1, 1]
    [filament] = plan.filaments
    assert (filament.slot_id, filament.group_id) == (3, 1)
    # Filament 3's colour, not the #F5547C slice_info.config wrote (filament 1's).
    assert filament.color == "#00AE42"
    assert filament.group is not None and filament.group.filament_color == "#00AE42"


def test_a_phantom_filament_1_joins_the_plates_first_filament() -> None:
    """#4712: plate 2 prints filaments 2 (rack, group 1) then 3 (fixed, group 0); the
    CLI's phantom ``id="1"`` (what it extruded before its first change) is filament 2's,
    in filament 2's group."""
    with (NATIVE / "plate2-two-filaments.gcode.3mf").open("rb") as file:
        plan = read_rack_plan(file, 2)
    assert plan is not None
    assert plan.slot_groups == [-1, 1, 0]
    assert [(f.slot_id, f.group_id, f.used_grams) for f in plan.filaments] == [
        (2, 1, 1.39),
        (3, 0, 1.1),
    ]


def test_a_phantom_filament_in_another_group_has_no_plan(caplog: pytest.LogCaptureFixture) -> None:
    """#4712: a phantom ``id="1"`` whose group is not the first filament's cannot be
    placed with confidence, so no field is sent."""
    phantom = (
        '<filament id="1" tray_info_idx="GFA00" type="PLA" color="#F5547C" used_m="0.05" '
        'used_g="0.15" group_id="{}"'
    )
    out = _rewritten(
        "plate2-two-filaments",
        lambda name, data: (
            data.replace(phantom.format(1).encode(), phantom.format(0).encode())
            if name == "Metadata/slice_info.config"
            else data
        ),
    )
    with caplog.at_level(logging.WARNING):
        assert read_rack_plan(out, 2) is None
    assert "nozzle_mapping not sent" in caplog.text


def _rewritten(fixture: str, edit: Callable[[str, bytes], bytes | None]) -> io.BytesIO:
    """A native slice fixture with each member as ``edit`` returns it (``None`` drops
    it), checked to differ from the original."""
    with zipfile.ZipFile(NATIVE / f"{fixture}.gcode.3mf") as original:
        members = {name: original.read(name) for name in original.namelist()}
    edited = {name: edit(name, data) for name, data in members.items()}
    assert edited != members
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in edited.items():
            if data is not None:
                archive.writestr(name, data)
    out.seek(0)
    return out


def test_a_phantom_with_no_first_filament_has_no_plan(caplog: pytest.LogCaptureFixture) -> None:
    """#4712: with no ``filament_sequence.json``, a header naming several filaments says
    nothing of which is first, so the phantom ``id="1"`` has no slot: no field."""
    out = _rewritten(
        "plate2-two-filaments",
        lambda name, data: None if name == "Metadata/filament_sequence.json" else data,
    )
    with caplog.at_level(logging.WARNING):
        assert read_rack_plan(out, 2) is None
    assert "has no first to join" in caplog.text


def test_a_corrupt_gcode_still_reads_the_plan_from_the_sequence() -> None:
    """#4712: a G-code member that cannot be inflated says nothing of the header, and the
    plan is read from ``filament_sequence.json`` alone, never refused for it."""
    payload = with_corrupt_member(
        (NATIVE / "plate3-one-filament.gcode.3mf").read_bytes(), "Metadata/plate_3.gcode"
    )
    plan = read_rack_plan(io.BytesIO(payload), 3)
    assert plan is not None
    assert plan.slot_groups == [-1, -1, 1]


def test_a_lone_filaments_type_is_its_slots_not_filament_1s() -> None:
    """#4712: the type feeds the abrasive check (`rack.rank.rack_groups`), so a lone
    filament's is the project's for its slot: here PLA-CF in slot 3, where
    ``slice_info.config`` wrote filament 1's PLA."""

    def carbon_in_slot_3(name: str, data: bytes) -> bytes:
        if name != "Metadata/project_settings.config":
            return data
        settings = json.loads(data)
        settings["filament_type"] = ["PLA", "PLA", "PLA-CF"]
        return json.dumps(settings).encode()

    plan = read_rack_plan(_rewritten("plate3-one-filament", carbon_in_slot_3), 3)
    assert plan is not None
    assert [(f.slot_id, f.type) for f in plan.filaments] == [(3, "PLA-CF")]


def test_a_project_with_no_colour_for_the_slot_has_no_plan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#4712: a lone filament takes its slot's type and colour from the project; a
    project that names none for that slot cannot say them, so no field is sent."""

    def two_colours(name: str, data: bytes) -> bytes:
        if name != "Metadata/project_settings.config":
            return data
        settings = json.loads(data)
        settings["filament_colour"] = settings["filament_colour"][:2]
        return json.dumps(settings).encode()

    with caplog.at_level(logging.WARNING):
        assert read_rack_plan(_rewritten("plate3-one-filament", two_colours), 3) is None
    assert "names no filament_colour for slot 3" in caplog.text
