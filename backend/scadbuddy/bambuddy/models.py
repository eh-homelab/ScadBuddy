"""Bambuddy wire shapes, recorded from a live 1.2.5.5 (see tests/bambuddy/recordings/).

Every model ignores unknown fields: Bambuddy's responses carry far more than
ScadBuddy needs and grow between releases. Ids are integers throughout — that is
Bambuddy's own OpenAPI, not a guess.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PresetSource = Literal["orca_cloud", "cloud", "local", "standard"]
SliceStatus = Literal["pending", "running", "completed", "failed"]


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


class Printer(BambuddyModel):
    id: int
    name: str
    model: str | None = None
    is_active: bool = True
    nozzle_count: int | None = None


class Folder(BambuddyModel):
    id: int
    name: str
    parent_id: int | None = None
    file_count: int | None = None


class Pipeline(BambuddyModel):
    id: int
    name: str
    description: str | None = None
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
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


class PipelineRun(BambuddyModel):
    id: int
    pipeline_id: int | None = None
    pipeline_name: str | None = None
    source_library_file_id: int | None = None
    copies: int = 1
    status: str
    slice_job_id: int | None = None
    sliced_library_file_id: int | None = None
    error_message: str | None = None


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
