"""What the profile-fetch worker and the CRUD service share (#6625): the queue, the
workflow's names, and the bodies of ``/api/v1/internal/crud/profile-fetch/…``. It imports
no database or app code, so the worker, which holds no database secret, can use it."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The task queue the profile-fetch worker polls, and the API starts fetches on.
DEFAULT_QUEUE: Final = "profile-fetch"
FETCH_WORKFLOW: Final = "ProfileFetch"
EXPORT_ACTIVITY: Final = "profile_fetch_export"
IMPORT_ACTIVITY: Final = "profile_fetch_import"
FAILED_ACTIVITY: Final = "profile_fetch_failed"
#: The CRUD areas the worker calls (`api/crud_areas.py` `CRUD_AREAS["profile-fetch"]`).
AREAS: Final = frozenset({"runs"})
#: The slicer option on the site: ScadBuddy slices only with Bambu Studio.
SLICER: Final = "Bambu Studio"
#: The largest preset file the CRUD service accepts.
MAX_FILE_BYTES: Final = 512 << 10
#: The problem ``code`` of a CRUD route for a run that is not ``running`` (Decision 25), and
#: the type of the worker's error for it, and for a run that is gone (``run_not_found``).
NOT_RUNNING: Final = "not_running"
MAX_FAILURE_REASON: Final = 300
MAX_FAILED_REASON: Final = 500


def fetch_workflow_id(run_id: str) -> str:
    """One workflow per run: the database allows one running run per type, and a new
    run never attaches to an ended run's workflow that still runs."""
    return f"profile-fetch:{run_id}"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportTarget(_Wire):
    """One printer model and the nozzle diameters to export for it."""

    printer_model: str
    nozzles: list[str]


class FetchAsk(_Wire):
    """The workflow's input: the run the API opened, the type's 3DFP id and the targets."""

    run_id: str
    type_id: str
    tdfp_id: int = Field(gt=0)
    targets: list[ExportTarget]


class ExportFailure(_Wire):
    printer_model: str
    nozzle: str
    reason: str = Field(max_length=MAX_FAILURE_REASON)


class ExportReport(_Wire):
    """What the export activity returns: files uploaded and the keys that failed."""

    exported: int = Field(ge=0)
    failures: list[ExportFailure]


class MissingKey(_Wire):
    printer_model: str
    nozzle_diameter: str
    reason: str


class ImportOutcome(_Wire):
    """What the import answers, and the workflow returns."""

    status: Literal["imported", "partial", "failed"]
    presets: int = Field(ge=0)
    k_values: int = Field(ge=0)
    missing: list[MissingKey]
    notes: list[str]


class FailedAsk(_Wire):
    """``POST …/runs/{run_id}/failed``: why the workflow gave up."""

    reason: str = Field(max_length=MAX_FAILED_REASON)
