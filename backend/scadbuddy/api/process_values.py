"""``/api/v1/print/process-values`` — every value of the process preset a print resolves
to (#2998): what the print dialog's "Slicer settings" editor lists, and what the agent's
``get_print_choices`` reads, so any Bambu Studio key can be found and set by name.

Read only. On ``slicer_backend`` ``native`` the image's BBL profiles answer (no Bambuddy
call, ``slicing/values.py``); otherwise Bambuddy's ``GET /slicer/preset-values``. Bambu
Studio's option definitions (labels, sections, types) are compiled into its binary and
are in neither the image nor the repo, so the answer is the keys and values alone.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.resolver import PrintChoices, process_preset_for
from scadbuddy.core.problems import ApiError
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.component import SlicingDep
from scadbuddy.slicing.values import values_for

router = APIRouter(prefix="/print/process-values", tags=["print"])


class ProcessValuesRequest(BaseModel):
    #: The print dialog's choices, which decide the process preset.
    choices: PrintChoices


class ProcessValues(BaseModel):
    #: The process preset the choices resolve to; ``None`` when none does.
    process: PresetRef | None = None
    #: Whether its values were read. ``False`` with ``reason`` and no values when they
    #: could not be: never an error, as filament profiles' previews answer.
    resolved: bool = False
    reason: str | None = None
    #: Every key of the flattened preset with its value: a string, or a list of strings
    #: for a per-extruder-variant option.
    values: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "",
    response_model=ProcessValues,
    summary="Every value of the process preset these choices resolve to",
)
async def process_values(
    body: ProcessValuesRequest, store: SettingsStoreDep, slicing: SlicingDep
) -> ProcessValues:
    """The resolved process preset's every key and value, flattened over its
    ``inherits:`` chain: the settings a print's ``process_overrides`` (#2998) can set, with
    the value each prints with when none is set. Any other Bambu Studio key may be set
    too; the slicer judges it."""
    settings = store.load()
    native = slicing.profiles if settings.slicer_backend == "native" else None
    async with client_for(settings) as client:
        reach = None if native is not None else client
        try:
            process = process_preset_for(body.choices, await catalogue_for(reach, native=native))
        except ApiError as error:
            return ProcessValues(reason=f"the presets could not be read: {error.detail}")
        if process is None:
            return ProcessValues(reason="no process preset resolves for these choices")
        try:
            answer = await values_for(reach, native=native)(process)
        except ApiError as error:
            return ProcessValues(process=process, reason=f"the read failed: {error.detail}")
    return ProcessValues(
        process=process, resolved=answer.resolved, reason=answer.reason, values=answer.values
    )
