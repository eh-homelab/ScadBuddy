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
  ``use_ams`` is false when the printer has no AMS, and otherwise only when the mapped
  filaments use external spools and no AMS tray (``SelectMachine.cpp:3358-3373``,
  ``obj_->HasAms()`` then ``_HasExt``/``_HasAms`` over the mapping).
- Bambuddy, which prints over LAN MQTT itself, sends the flat array as one global tray
  id per slot: ``ams_id * 4 + slot_id`` for an AMS, the AMS-HT's own id at 128+, the
  A2L AMS-Lite's local slot (unit 16, see ``AMS_LITE_ID``), and
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
#: The unit ids a printer reports (Bambuddy ``bambu_mqtt.py:121-127`` at ``0d427510``: "regular
#: AMS 0-3, AMS-HT 128-135", and the A2L's AMS-Lite at 16): one AMS-HT tray per unit.
AMS_IDS = range(0, 4)
HT_IDS = range(128, 136)
#: The A2L's AMS-Lite, which the wire names by its own unit id with a local slot 0-3, in
#: both arrays: "CONFIRMED from the firmware's own `ams_mapping2` ({ams_id:16, slot_id:0-3})"
#: and the flat value the local slot (``bambu_mqtt.py:115``, ``:158-179``, ``:6020-6027``).
AMS_LITE_ID = 16
TRAYS_PER_AMS = 4


class AmsSlot(BaseModel):
    ams_id: int
    slot_id: int


class AmsMapping(BaseModel):
    #: Per filament slot: the global tray id; -1 for a slot no tray feeds or an external spool.
    ams_mapping: list[int]
    #: Per filament slot: ``{ams_id, slot_id}``; ``{255, 255}`` unmapped; external ``{255|254, 0}``.
    ams_mapping2: list[AmsSlot]
    #: False when the printer has no AMS, or the mapped slots use external spools and no
    #: AMS tray.
    use_ams: bool


def _slot(ams_id: int, tray_id: int, *, dual_nozzle: bool) -> tuple[int, AmsSlot]:
    if ams_id == EXTERNAL and tray_id in (0, 1):
        side = VIRTUAL_TRAY_DEPUTY_ID + tray_id if dual_nozzle else VIRTUAL_TRAY_MAIN_ID
        return -1, AmsSlot(ams_id=side, slot_id=0)
    if ams_id in HT_IDS and tray_id == 0:
        return ams_id, AmsSlot(ams_id=ams_id, slot_id=0)
    if ams_id == AMS_LITE_ID and 0 <= tray_id < TRAYS_PER_AMS:
        return tray_id, AmsSlot(ams_id=ams_id, slot_id=tray_id)
    if ams_id in AMS_IDS and 0 <= tray_id < TRAYS_PER_AMS:
        return ams_id * TRAYS_PER_AMS + tray_id, AmsSlot(ams_id=ams_id, slot_id=tray_id)
    raise ValueError(f"no such tray: AMS {ams_id} tray {tray_id}")


def ams_mapping(
    plan: FilamentPlan,
    project_filament_count: int,
    *,
    dual_nozzle: bool,
    printer_has_ams: bool,
) -> AmsMapping:
    """The mapping for ``plan``'s trays.

    ``project_filament_count`` is the number of filament slots the *project* has, at
    least its largest slot id, not the number the plate prints with: Bambu Studio sizes
    both arrays by the project's filaments (``SelectMachine.cpp:1463``,
    ``filament_presets.size()``) and Bambuddy by the largest slot used
    (``print_scheduler.py:4180-4184`` at ``0d427510``), while a sliced plate lists only
    the filaments it uses (``bambuddy/filaments3mf.py``). A slot the plan leaves out is
    unmapped. ``printer_has_ams`` is whether the printer's report has any AMS unit:
    without one, ``use_ams`` is false whatever is mapped (``SelectMachine.cpp:3358-3373``,
    the ``else`` at ``:3371-3373``; Bambuddy: "P1S/P1P with no AMS rejects use_ams=True",
    ``bambu_mqtt.py:6056-6058``), and an AMS tray in the plan is refused. Otherwise
    ``use_ams`` follows the printer unless the plan maps external spools and no AMS
    tray, so a plan that maps nothing on an AMS printer has it true (the vector "an
    AMS printer, nothing mapped", #4177). A positive (inventory) spool id raises
    ``ValueError``: a native plan is trays only. A ``project_filament_count`` below 1
    also raises ``ValueError``: every project has a filament slot (#4204)."""
    count = project_filament_count
    if count < 1:
        raise ValueError(f"a project has at least one filament slot, not {count}")
    flat = [-1] * count
    detail = [AmsSlot(ams_id=UNMAPPED, slot_id=UNMAPPED) for _ in range(count)]
    seen: set[int] = set()
    has_ams = has_external = False
    for choice in plan.slots:
        if not 1 <= choice.slot_id <= count:
            raise ValueError(f"slot {choice.slot_id} is not one of the project's {count}")
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
        elif not printer_has_ams:
            raise ValueError(f"no such tray: AMS {tray[0]} on a printer with no AMS")
        else:
            has_ams = True
    use_ams = printer_has_ams and not (has_external and not has_ams)
    return AmsMapping(ams_mapping=flat, ams_mapping2=detail, use_ams=use_ams)
