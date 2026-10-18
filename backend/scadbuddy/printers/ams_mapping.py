"""``ams_mapping`` and ``ams_mapping2`` from a native plan's trays (#3818 tier A, Task 7).

A LAN print's ``print.project_file`` says, per filament slot of the sliced plate, which
tray feeds it. ScadBuddy has never computed this (Bambuddy's scheduler did, for a queue
item without one); a native run must. The encoding, from two sources that agree:

- Bambu Studio sends both arrays (``src/slic3r/GUI/Jobs/PrintJob.cpp:263-264`` at tag
  ``v02.08.02.61``). ``ams_mapping2`` is one ``{ams_id, slot_id}`` per filament slot,
  ``{255, 255}`` (``0xff``) for a slot no tray feeds; the flat ``ams_mapping`` is the
  tray id, with the virtual (external) tray ids turned into ``-1``
  (``src/slic3r/GUI/SelectMachine.cpp:1463-1504``; ``VIRTUAL_TRAY_MAIN_ID`` 255 and
  ``VIRTUAL_TRAY_DEPUTY_ID`` 254, ``src/slic3r/GUI/DeviceCore/DevDefs.h:86-87``).
  ``use_ams`` is false only when the mapped filaments use external spools and no AMS
  tray (``SelectMachine.cpp:3358-3373``, ``_HasExt``/``_HasAms`` over the mapping).
- Bambuddy, which prints over LAN MQTT itself, sends the flat array as one global tray
  id per slot: ``ams_id * 4 + slot_id`` for an AMS, the AMS-HT's own id at 128+, and
  ``-1`` for an unmapped slot *and* for an external spool, since "firmware doesn't
  accept raw 254/255" there (the H2D failure ``0700_8012``); ``ams_mapping2`` carries
  the external spool as ``{ams_id: 255, slot_id: 0}`` on a single-nozzle printer and
  ``254``/``255`` by side on a dual-nozzle one (deputy/left 254, main/right 255)
  (``backend/app/services/bambu_mqtt.py:5987-6033`` and ``:6172-6174`` at
  maziggy/bambuddy ``0d427510``, read 2026-10-18).
- OpenBambuAPI ``mqtt.md`` (``cc383a2c``, "AMS Mapping Configuration") describes a fixed
  five-element array filled from the right. It matches neither and is not followed.

Trays are numbered as ``bambuddy/trays.py`` numbers them: ``(ams_id, tray_id)`` from
``tray_of_spool_id``, the external holders ``ams_id`` 255 with tray 0 (Ext-L,
``vt_tray`` id 254) and tray 1 (Ext-R, ``vt_tray`` id 255). On a single-nozzle printer
either is sent as 255, as Bambuddy does: the printer reports 254 for its one holder,
and Bambu Studio sends 255.

Not yet measured on a printer: the plan's Task 7 Step 1 (one two-filament plate sent
from Bambu Studio over LAN, its ``project_file`` captured) happens in Task 8's session
at the H2C with the user. Until then this encoding rests on the two sources above.
"""

from pydantic import BaseModel

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.trays import EXTERNAL, tray_of_spool_id

#: ``ams_mapping2``'s "no tray feeds this slot" (Bambu Studio's ``0xff``).
UNMAPPED = 255
#: Bambu Studio's virtual tray ids: the main (right) extruder's holder and the deputy's.
VIRTUAL_TRAY_MAIN_ID = 255
VIRTUAL_TRAY_DEPUTY_ID = 254
#: An AMS id at or above this is an AMS-HT, one tray per unit (``trays.unit_label``).
HT_BASE = 128
TRAYS_PER_AMS = 4


class AmsSlot(BaseModel):
    ams_id: int
    slot_id: int


class AmsMapping(BaseModel):
    #: Per filament slot: the global tray id; -1 for a slot no tray feeds or an external spool.
    ams_mapping: list[int]
    #: Per filament slot: ``{ams_id, slot_id}``; ``{255, 255}`` unmapped; external ``{255|254, 0}``.
    ams_mapping2: list[AmsSlot]
    #: False only when the mapped slots use external spools and no AMS tray.
    use_ams: bool


def _slot(ams_id: int, tray_id: int, *, dual_nozzle: bool) -> tuple[int, AmsSlot]:
    if ams_id == EXTERNAL and tray_id in (0, 1):
        side = VIRTUAL_TRAY_DEPUTY_ID + tray_id if dual_nozzle else VIRTUAL_TRAY_MAIN_ID
        return -1, AmsSlot(ams_id=side, slot_id=0)
    if HT_BASE <= ams_id < EXTERNAL - 1 and tray_id == 0:
        return ams_id, AmsSlot(ams_id=ams_id, slot_id=0)
    if 0 <= ams_id < HT_BASE and 0 <= tray_id < TRAYS_PER_AMS:
        return ams_id * TRAYS_PER_AMS + tray_id, AmsSlot(ams_id=ams_id, slot_id=tray_id)
    raise ValueError(f"no such tray: AMS {ams_id} tray {tray_id}")


def ams_mapping(plan: FilamentPlan, filament_count: int, *, dual_nozzle: bool) -> AmsMapping:
    """The mapping for a plate of ``filament_count`` filament slots (from the sliced
    plate) printed with ``plan``'s trays. A slot the plan leaves out is unmapped. A
    positive (inventory) spool id raises ``ValueError``: a native plan is trays only."""
    flat = [-1] * filament_count
    detail = [AmsSlot(ams_id=UNMAPPED, slot_id=UNMAPPED) for _ in range(filament_count)]
    seen: set[int] = set()
    has_ams = has_external = False
    for choice in plan.slots:
        if not 1 <= choice.slot_id <= filament_count:
            raise ValueError(f"slot {choice.slot_id} is not one of the plate's {filament_count}")
        if choice.slot_id in seen:
            raise ValueError(f"slot {choice.slot_id} is chosen twice")
        seen.add(choice.slot_id)
        tray = tray_of_spool_id(choice.spool_id)
        if tray is None:
            raise ValueError(
                f"spool {choice.spool_id} is an inventory spool; a native print takes a tray"
            )
        index = choice.slot_id - 1
        flat[index], detail[index] = _slot(*tray, dual_nozzle=dual_nozzle)
        if tray[0] == EXTERNAL:
            has_external = True
        else:
            has_ams = True
    return AmsMapping(
        ams_mapping=flat, ams_mapping2=detail, use_ams=not (has_external and not has_ams)
    )
