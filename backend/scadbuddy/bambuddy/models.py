"""Bambuddy wire shapes, recorded from a live 1.2.5.5 (see tests/bambuddy/recordings/).

Every model ignores unknown fields: Bambuddy's responses carry far more than
ScadBuddy needs and grow between releases. Ids are integers throughout — that is
Bambuddy's own OpenAPI, not a guess.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PresetSource = Literal["orca_cloud", "cloud", "local", "standard"]
SliceStatus = Literal["pending", "running", "completed", "failed"]
#: ``bed_levelling``, ``flow_cali`` and ``nozzle_offset_cali`` on the queue route.
CalibrationMode = Literal["off", "on", "auto"]
PreheatOverride = Literal["inherit", "on", "off"]
TargetKind = Literal["specific_printer", "printer_class"]
FanoutStrategy = Literal["max_parallel", "fill_one_first", "round_robin"]


class BambuddyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PresetRef(BambuddyModel):
    """A slicer preset, identified by tier and id (``{"source":"cloud","id":"GM041"}``)."""

    source: PresetSource
    id: str


class Preset(PresetRef):
    name: str | None = None
    filament_type: str | None = None
    filament_colour: str | None = None
    compatible_printers: list[str] | None = None


class PresetTier(BambuddyModel):
    printer: list[Preset] = Field(default_factory=list)
    process: list[Preset] = Field(default_factory=list)
    filament: list[Preset] = Field(default_factory=list)


class PresetCatalogue(BambuddyModel):
    """``GET /api/v1/slicer/presets`` — one tier per source, plus their auth status."""

    cloud: PresetTier = Field(default_factory=PresetTier)
    standard: PresetTier = Field(default_factory=PresetTier)
    local: PresetTier = Field(default_factory=PresetTier)
    orca_cloud: PresetTier = Field(default_factory=PresetTier)
    cloud_status: str | None = None
    orca_cloud_status: str | None = None


class LocalPreset(BambuddyModel):
    """A row of ``GET /api/v1/local-presets/`` — presets imported from an OrcaSlicer
    profile rather than fetched from a cloud tier.

    Note the type difference against :class:`Preset`: ``compatible_printers`` is a
    **JSON-encoded string** here (``'["Bambu Lab H2C 0.4 nozzle"]'``), not a list, and
    so is ``default_filament_colour``. Bambuddy stores these columns verbatim.

    The ``id`` is an integer DB row id; a :class:`PresetRef` to it stringifies that id
    and uses ``source="local"``.
    """

    id: int
    name: str
    preset_type: str
    source: str
    filament_type: str | None = None
    filament_vendor: str | None = None
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    default_filament_colour: str | None = None
    compatible_printers: str | None = None
    inherits: str | None = None

    def ref(self) -> PresetRef:
        return PresetRef(source="local", id=str(self.id))


class LocalPresetCatalogue(BambuddyModel):
    """``GET /api/v1/local-presets/`` — grouped by type, not by source tier."""

    filament: list[LocalPreset] = Field(default_factory=list)
    printer: list[LocalPreset] = Field(default_factory=list)
    process: list[LocalPreset] = Field(default_factory=list)


class Printer(BambuddyModel):
    """``GET /api/v1/printers/`` and ``GET /api/v1/printers/{id}`` — the same shape.

    ``access_code`` is deliberately absent: Bambuddy withholds it from API-keyed
    callers, so a model that carried it would be null in production and populated
    only when auth is disabled.
    """

    id: int
    name: str
    model: str | None = None
    is_active: bool = True
    nozzle_count: int | None = None


class NozzleInfo(BambuddyModel):
    """``nozzle_diameter`` is a **string** here ("0.4"), unlike the float the queue
    route reports back on a print."""

    nozzle_type: str = ""
    nozzle_diameter: str = ""


class NozzleRackSlot(NozzleInfo):
    """A slot of an H2-series nozzle rack — what ``nozzle_rack_choice`` picks from."""

    id: int = 0
    wear: int | None = None
    stat: int | None = None
    filament_type: str = ""
    filament_colour: str = Field(default="", alias="filament_color")

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AmsTray(BambuddyModel):
    """One AMS slot. ``remain`` is ``-1`` when the spool is not RFID-tagged, and the
    external spool arrives through ``PrinterStatus.vt_tray`` rather than an AMS."""

    id: int
    tray_color: str | None = None
    tray_type: str | None = None
    tray_sub_brands: str | None = None
    tray_id_name: str | None = None
    tray_info_idx: str | None = None
    remain: int = 0
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    state: int | None = None
    exists: bool | None = None


class AmsUnit(BambuddyModel):
    """One AMS. ``id`` is the printer's own numbering and is **not** a list index: a
    single-slot AMS-HT reports ``id: 128`` and the units arrive unsorted, so an
    ``ams[n]`` lookup finds the wrong unit. Match on ``id``.
    """

    id: int
    humidity: int | None = None
    temp: float | None = None
    is_ams_ht: bool = False
    module_type: str = ""
    tray: list[AmsTray] = Field(default_factory=list)


class PrinterStatus(BambuddyModel):
    """``GET /api/v1/printers/{id}/status`` — live MQTT state, 60-odd fields deep.

    Only what a print decision needs is modelled. Two shapes are easy to get wrong:
    ``ams_switch_inlet`` is keyed by **stringified** AMS id (JSON has no integer keys),
    and ``nozzles`` is indexed by extruder, so ``nozzles[active_extruder]`` is the one
    that will print.
    """

    id: int
    name: str
    connected: bool
    state: str | None = None
    ams: list[AmsUnit] = Field(default_factory=list)
    ams_exists: bool = False
    vt_tray: list[AmsTray] = Field(default_factory=list)
    nozzles: list[NozzleInfo] = Field(default_factory=list)
    nozzle_rack: list[NozzleRackSlot] = Field(default_factory=list)
    active_extruder: int = 0
    ams_mapping: list[int] = Field(default_factory=list)
    ams_switch_inlet: dict[str, str] = Field(default_factory=dict)
    current_plate_id: int | None = None


class AvailableFilament(BambuddyModel):
    """``GET /api/v1/printers/available-filaments?model=`` — deduplicated across every
    active printer of that model. ``model`` is a required query parameter.

    ``color`` here is ``#RRGGBBAA`` with a leading ``#``, while the AMS tray it came
    from spells the same colour ``RRGGBBAA`` without one.
    """

    type: str | None = None
    color: str | None = None
    tray_info_idx: str | None = None
    tray_sub_brands: str | None = None
    extruder_id: int | None = None


class Folder(BambuddyModel):
    id: int
    name: str
    parent_id: int | None = None
    file_count: int | None = None
    project_id: int | None = None
    project_name: str | None = None
    archive_id: int | None = None
    is_external: bool = False


class FolderCreate(BambuddyModel):
    """``POST /api/v1/library/folders/`` — note the trailing slash.

    The slashless ``/library/folders`` also exists and is the one ``folders()`` reads;
    both are real routes on 1.2.5.5, unlike ``/printers``.
    """

    name: str
    parent_id: int | None = None
    project_id: int | None = None


class Pipeline(BambuddyModel):
    id: int
    name: str
    description: str | None = None
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
    bed_type: str | None = None
    target_kind: TargetKind = "printer_class"
    target_printer_id: int | None = None
    target_model_class: str | None = None
    fanout_strategy: FanoutStrategy = "max_parallel"


class PipelineCreate(BambuddyModel):
    """``POST /api/v1/slicer-pipelines/``.

    ``SlicerPipelineCreate`` carries **no** target or fanout fields even though
    ``SlicerPipelineResponse`` returns them — a pipeline is created against the
    defaults and re-targeted with ``PUT``, which ScadBuddy does not do.
    """

    name: str
    description: str | None = None
    printer_preset: PresetRef
    process_preset: PresetRef
    #: One per AMS slot, in the source plate's filament-slot order. Bambuddy rejects
    #: an empty list (``minItems: 1``).
    filament_presets: list[PresetRef]
    bed_type: str | None = None


class PipelineList(BambuddyModel):
    pipelines: list[Pipeline] = Field(default_factory=list)


class LibraryFile(BambuddyModel):
    """``POST /api/v1/library/files`` — the uploaded file."""

    id: int
    filename: str
    file_type: str | None = None
    file_size: int | None = None
    thumbnail_path: str | None = None
    duplicate_of: int | None = None


class SliceRequest(BambuddyModel):
    """``POST /api/v1/library/files/{id}/slice``.

    Note ``plate``, not ``plate_id`` — the queue route is the one that spells it
    ``plate_id``.
    """

    printer_preset: PresetRef
    process_preset: PresetRef
    filament_presets: list[PresetRef] = Field(default_factory=list)
    filament_colours: list[str] = Field(default_factory=list)
    bed_type: str | None = None
    plate: int = 1
    use_embedded_settings: bool = False


class SliceJobAccepted(BambuddyModel):
    job_id: int
    status: str | None = None
    status_url: str | None = None


class SliceResult(BambuddyModel):
    library_file_id: int | None = None
    name: str | None = None
    print_time_seconds: float | None = None
    filament_used_g: float | None = None


class SliceJob(BambuddyModel):
    """``GET /api/v1/slice-jobs/{id}``. Bambuddy leaves this one untyped in its own
    OpenAPI, so every field past ``status`` is optional."""

    id: int | None = None
    status: SliceStatus | str
    error: str | None = None
    error_message: str | None = None
    result: SliceResult | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed")

    @property
    def failure(self) -> str | None:
        if self.status != "failed":
            return None
        return self.error or self.error_message or "Bambuddy did not say why"


class PipelineJob(BambuddyModel):
    """One copy of a run: the queue entry it became and the printer it landed on."""

    id: int
    pipeline_run_id: int
    copy_index: int
    assigned_printer_id: int | None = None
    assigned_printer_name: str | None = None
    queue_entry_id: int | None = None
    status: str
    error_message: str | None = None


class PipelineRun(BambuddyModel):
    id: int
    pipeline_id: int | None = None
    pipeline_name: str | None = None
    source_library_file_id: int | None = None
    source_archive_id: int | None = None
    source_filename: str | None = None
    copies: int = 1
    copies_completed: int = 0
    copies_failed: int = 0
    copies_cancelled: int = 0
    copies_in_progress: int = 0
    status: str
    slice_job_id: int | None = None
    sliced_library_file_id: int | None = None
    eligibility_overridden: bool = False
    error_message: str | None = None
    jobs: list[PipelineJob] = Field(default_factory=list)
    target_kind: TargetKind | None = None
    target_printer_id: int | None = None
    target_model_class: str | None = None
    fanout_strategy: FanoutStrategy | None = None


class PipelineRunList(BambuddyModel):
    runs: list[PipelineRun] = Field(default_factory=list)
    total: int = 0


class PipelineRunRequest(BambuddyModel):
    """``POST /api/v1/slicer-pipelines/{id}/run``.

    Exactly one source must be set. ``force`` turns the 409 eligibility refusal into
    a run that records ``eligibility_overridden``.
    """

    source_library_file_id: int | None = None
    source_archive_id: int | None = None
    copies: int = 1
    force: bool = False


class EligibilityRequest(BambuddyModel):
    """``POST /api/v1/slicer-pipelines/{id}/check-eligibility``. One source, as above."""

    source_library_file_id: int | None = None
    source_archive_id: int | None = None
    force: bool = False


class EligibilityIssue(BambuddyModel):
    """``kind`` is an open enum here on purpose — Bambuddy adds kinds between
    releases and an unknown one must still render, not 502 the whole report."""

    kind: str
    slot_index: int | None = None
    expected: str | None = None
    actual: str | None = None


class PerPrinterReport(BambuddyModel):
    printer_id: int
    printer_name: str
    ok: bool
    issues: list[EligibilityIssue] = Field(default_factory=list)


class EligibilityReport(BambuddyModel):
    """Returned by ``check-eligibility`` and, on a 409, by ``run``.

    Under ``target_kind="printer_class"`` ``ok`` is true when *at least one* matching
    printer passes, and the per-printer detail moves to ``printer_reports`` — ``issues``
    then carries only class-level problems. Reading ``ok`` as "every printer is ready"
    is wrong for that target kind.
    """

    ok: bool
    target_kind: TargetKind = "specific_printer"
    target_printer_id: int | None = None
    target_printer_name: str | None = None
    target_model_class: str | None = None
    issues: list[EligibilityIssue] = Field(default_factory=list)
    printer_reports: list[PerPrinterReport] = Field(default_factory=list)


class QueueItem(BambuddyModel):
    id: int
    printer_id: int | None = None
    library_file_id: int | None = None
    position: int | None = None
    status: str | None = None
    plate_id: int | None = None


class ExternalLink(BambuddyModel):
    id: int
    name: str
    url: str
    icon: str = "link"
    open_in_new_tab: bool = False
    sort_order: int | None = None


class Project(BambuddyModel):
    """``GET /api/v1/projects/`` and ``GET|POST /api/v1/projects/{id}``.

    One model for both: the list route returns roll-up counters the single-project
    route does not, so they default rather than being required.
    """

    id: int
    name: str
    description: str | None = None
    color: str | None = None
    status: str
    priority: str = "normal"
    tags: str | None = None
    url: str | None = None
    parent_id: int | None = None
    archive_count: int = 0
    queue_count: int = 0
    total_items: int = 0
    completed_count: int = 0


class ProjectCreate(BambuddyModel):
    """``POST /api/v1/projects/``. ``tags`` is a comma-separated string, not a list."""

    name: str
    description: str | None = None
    color: str | None = None
    tags: str | None = None
    priority: str = "normal"
    parent_id: int | None = None
    url: str | None = None


class QueueItemCreate(BambuddyModel):
    """``POST /api/v1/queue/`` — Bambuddy's ``PrintQueueItemCreate`` in full.

    Every default here is Bambuddy's own, so an unset field and an omitted one mean the
    same thing. Two are worth stating because the earlier ScadBuddy send overrode them:
    ``bed_levelling`` and ``flow_cali`` default to ``"auto"``, and they are three-way
    enums — a bool is a 422.

    ``variants`` is the one field of the upstream schema left out: it drives multi-colour
    variant expansion, which nothing in ScadBuddy produces.
    """

    printer_id: int | None = None
    #: Model-based assignment: set these *instead* of ``printer_id`` to let Bambuddy
    #: pick any printer of that model.
    target_model: str | None = None
    target_location: str | None = None
    required_filament_types: list[str] | None = None
    #: Per-slot overrides, passed through as Bambuddy defines them.
    filament_overrides: list[dict[str, Any]] | None = None
    archive_id: int | None = None
    library_file_id: int | None = None
    scheduled_time: datetime | None = None
    require_previous_success: bool = False
    auto_off_after: bool = False
    manual_start: bool = False
    insert_at_top: bool = False
    insert_position: int | None = None
    skip_filament_check: bool = False
    #: One AMS slot index per filament slot of the plate.
    ams_mapping: list[int] | None = None
    plate_id: int | None = None
    bed_levelling: CalibrationMode = "auto"
    flow_cali: CalibrationMode = "auto"
    vibration_cali: bool = True
    layer_inspect: bool = False
    timelapse: bool = False
    use_ams: bool = True
    nozzle_offset_cali: CalibrationMode = "auto"
    preheat_override: PreheatOverride = "inherit"
    preheat_chamber_target_override: int | None = None
    gcode_injection: bool = False
    quantity: int = 1
    batch_id: int | None = None
    project_id: int | None = None
    cost_center_id: int | None = None
    estimated_cost: float | None = None
    #: Nozzle-rack slot per extruder, keyed by stringified extruder index.
    nozzle_rack_choice: dict[str, int] | None = None
    cleanup_library_after_dispatch: bool = False
