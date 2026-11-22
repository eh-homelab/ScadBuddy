"""The check's rack preview finds the spool each rack-side slot names, for Glow (#6872).

The owner's dialog (2026-11-21, printer 3DP-31B-598, library file 1000000763) said "A
spool is not in the inventory, so Glow could not be checked." Its check ran the native
path with the plan ``{"slots": [{"slot_id": 2, "spool_id": -2049}], "ids": "sb"}``
(HT-A chosen for itself, read from its Temporal history), and the preview looked the
id up among SB numbers only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from scadbuddy.bambuddy import print_run
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import (
    AmsTray,
    AmsUnit,
    NozzleChoice,
    PrinterStatus,
    SlotChoice,
)
from scadbuddy.bambuddy.nozzle_plan import NozzlePlan, PlannedSlot
from scadbuddy.bambuddy.print_run import (
    PrintRunRequest,
    RackPickView,
    RackUncheckedSlot,
    rack_preview,
)
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.filament.models import FilamentInventorySpool, TrayHolder, TrayPlace, TrayView
from scadbuddy.library.settings_store import StoredSettings
from tests.rack.helpers import mounted, slot
from tests.support.sb_rows import sb_spool

HT_A = tray_spool_id(128, 0)


def _status(*, filled: bool = True) -> PrinterStatus:
    tray = AmsTray(id=0, tray_type="PETG" if filled else "", tray_color="A7AEB2FF")
    return PrinterStatus(
        id=1,
        name="H2C",
        connected=True,
        nozzle_rack=[mounted(diameter="0.4"), slot(1), slot(2)],
        ams=[AmsUnit(id=128, is_ams_ht=True, tray=[tray])],
    )


def _request(*slots: SlotChoice) -> PrintRunRequest:
    return PrintRunRequest(
        filament_plan=FilamentPlan(slots=list(slots), ids="sb"),
        choices=PrintChoices(nozzles=[NozzleChoice(size="0.4", flow="standard")]),
        rack_algorithm="least_used",
    )


async def _preview(
    request: PrintRunRequest,
    *,
    status: PrinterStatus | None = None,
    inventory: list[FilamentInventorySpool] | None = None,
    held: TrayView | None = None,
    nozzle_plan: NozzlePlan | None = None,
) -> RackPickView:
    view, _ = await rack_preview(
        None,
        request,
        StoredSettings(),
        printer_id=1,
        status=status or _status(),
        rack=None,
        laid_out=False,
        nozzle_plan=nozzle_plan,
        inventory=inventory,
        held=held,
    )
    assert view is not None
    return view


def _held(spool_number: int | None, subtype: str | None = None) -> TrayView:
    spool = (
        sb_spool(spool_number, material="PETG", subtype=subtype)
        if spool_number is not None
        else None
    )
    return TrayView(
        trays=[TrayHolder(ams_id=128, tray_id=0, spool=spool, found_by="link")],
        shelf=[],
    )


async def test_the_native_check_finds_the_sb_spool_in_the_tray_the_plan_names() -> None:
    """The reproduced case: the native check reads no inventory, and the slot names HT-A
    by its tray id. The SB spool in that tray is the one Glow is checked on."""
    view = await _preview(
        _request(SlotChoice(slot_id=2, spool_id=HT_A)), inventory=[], held=_held(7)
    )

    assert view.glow_unchecked is False
    assert view.unchecked == []


async def test_a_glow_spool_in_the_named_tray_is_judged_abrasive() -> None:
    view = await _preview(
        _request(SlotChoice(slot_id=2, spool_id=HT_A)), inventory=[], held=_held(7, "Glow")
    )

    assert view.unsafe_material is True
    assert view.glow_unchecked is False


async def test_the_bambuddy_check_finds_the_inventory_spool_in_the_named_tray() -> None:
    row = FilamentInventorySpool(
        spool=sb_spool(7, material="PETG", subtype="Glow"),
        loaded=TrayPlace(
            printer_id=None, bambuddy_printer_id=1, printer_name="H2C", ams_id=128, tray_id=0
        ),
    )

    view = await _preview(_request(SlotChoice(slot_id=2, spool_id=HT_A)), inventory=[row])

    assert view.glow_unchecked is False
    assert view.unsafe_material is True


async def test_an_sb_number_the_inventory_lacks_is_named() -> None:
    view = await _preview(_request(SlotChoice(slot_id=2, spool_id=45)), inventory=[])

    assert view.glow_unchecked is True
    assert view.unchecked == [
        RackUncheckedSlot(slot_id=2, reason="not_in_inventory", spool_number=45)
    ]


async def test_a_filled_tray_with_no_inventory_spool_is_named_by_tray_and_colour() -> None:
    view = await _preview(
        _request(SlotChoice(slot_id=2, spool_id=HT_A)), inventory=[], held=_held(None)
    )

    assert view.unchecked == [
        RackUncheckedSlot(
            slot_id=2,
            reason="not_in_inventory",
            tray="HT-A",
            material="PETG",
            colour="#A7AEB2",
            colour_word="light grey",
        )
    ]


async def test_an_empty_tray_says_it_holds_no_spool() -> None:
    view = await _preview(
        _request(SlotChoice(slot_id=2, spool_id=HT_A)),
        status=_status(filled=False),
        inventory=[],
        held=_held(None),
    )

    assert view.unchecked == [RackUncheckedSlot(slot_id=2, reason="empty_tray", tray="HT-A")]


async def test_a_rack_side_slot_with_no_spool_chosen_says_so() -> None:
    plan = NozzlePlan(
        slots=[PlannedSlot(slot_id=3, side="R", name="Slot 3", colour="#FFFFFF", material="PLA")],
        size="0.4",
        left_flow="standard",
        right_flow="standard",
        summary="",
    )

    view = await _preview(_request(), inventory=[], nozzle_plan=plan)

    assert view.unchecked == [
        RackUncheckedSlot(
            slot_id=3, reason="no_spool", material="PLA", colour="#FFFFFF", colour_word="white"
        )
    ]


class _StopError(Exception):
    """Ends the native check once its rack preview ran: the rest needs the image."""


async def test_the_native_check_passes_the_trays_sb_spools_to_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's case through `_native_check` (#6872): HT-A holds an SB Glow spool,
    the plan names HT-A by its tray id, and the preview finds and tests that spool."""
    status = _status()
    previews: list[RackPickView | None] = []
    real_preview = print_run.rack_preview

    async def prepared(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(printer_status=status, printer_id=1, printer="p", warnings=[])

    async def preview(*args: Any, **kwargs: Any) -> Any:
        found = await real_preview(*args, **kwargs)
        previews.append(found[0])
        raise _StopError

    class Trays:
        def trays(self, printer_id: UUID, seen: object) -> TrayView:
            return _held(7, "Glow")

    class Source:
        native = None
        is_sliced = False

        async def states_nozzles(self, _client: object) -> bool:
            return False

    monkeypatch.setattr(print_run, "prepare_run", prepared)
    monkeypatch.setattr(print_run, "rack_preview", preview)
    route = SimpleNamespace(row=SimpleNamespace(id=str(uuid4())), status=status)

    with pytest.raises(_StopError):
        await print_run._native_check(
            cast(Any, Source()),
            StoredSettings(),
            _request(SlotChoice(slot_id=2, spool_id=HT_A)),
            cast(Any, route),
            rack=None,
            profiles=cast(Any, object()),
            sb_trays=cast(Any, Trays()),
        )

    [view] = previews
    assert view is not None
    assert view.unchecked == []
    assert view.unsafe_material is True
