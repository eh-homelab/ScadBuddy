"""Remembered print options — Bambuddy's own queue-item option fields, nothing invented.

Every field here is named exactly as ``PrintQueueItemCreate`` names it, and
:func:`queue_fields` hands the set ones straight to :class:`QueueItemCreate`. A test
asserts the name sets still line up, so a Bambuddy rename shows up as a red test
rather than as a silently dropped option.

**Unset means "let Bambuddy decide".** ScadBuddy does not hold a second copy of
Bambuddy's defaults and never sends a value it was not given; :data:`BAMBUDDY_DEFAULTS`
exists only so the UI can say which of the user's choices differ from them, and the
request body it is serialised into is never sent to Bambuddy.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.models import CalibrationMode, PreheatOverride

#: The scopes an override can be remembered at, least to most specific. ``request`` is
#: not one of them — it is not remembered.
OptionScope = Literal["global", "printer", "model"]

#: The one option ``POST /slicer-pipelines/{id}/run`` can express, as its ``copies``.
#: Everything else has to go out on the queue route; see :mod:`scadbuddy.bambuddy.send`.
PIPELINE_CARRIED = frozenset({"quantity"})


class PrintOptions(BaseModel):
    """A sparse overlay on ``PrintQueueItemCreate``'s option fields.

    ``extra="forbid"`` on purpose: a misspelled option must 422 rather than be
    remembered under a name nothing will ever read.
    """

    model_config = ConfigDict(extra="forbid")

    bed_levelling: CalibrationMode | None = None
    flow_cali: CalibrationMode | None = None
    vibration_cali: bool | None = None
    nozzle_offset_cali: CalibrationMode | None = None
    layer_inspect: bool | None = None
    timelapse: bool | None = None
    use_ams: bool | None = None
    quantity: int | None = Field(default=None, ge=1, le=1000)
    manual_start: bool | None = None
    insert_at_top: bool | None = None
    auto_off_after: bool | None = None
    project_id: int | None = None
    preheat_override: PreheatOverride | None = None
    #: Bambuddy bounds this one itself (0-65); mirrored so a bad value 422s here.
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=65)

    def queue_fields(self) -> dict[str, Any]:
        """The set options, keyed as ``QueueItemCreate`` spells them."""
        return {
            name: value
            for name, value in self.model_dump(mode="python").items()
            if value is not None
        }

    def is_empty(self) -> bool:
        return not self.queue_fields()

    def beyond_pipeline(self) -> tuple[str, ...]:
        """The set options a pipeline run cannot carry, sorted.

        Non-empty means the send has to slice and queue rather than run the pipeline —
        ``POST /slicer-pipelines/{id}/run`` takes no option fields at all and creates
        its queue entries later, in a background task, from Bambuddy's own defaults.
        """
        return tuple(sorted(set(self.queue_fields()) - PIPELINE_CARRIED))


#: Bambuddy 1.2.5.5's own ``PrintQueueItemCreate`` defaults, recorded from its
#: ``openapi.json``. ``project_id`` and ``preheat_chamber_target_override`` are absent
#: because Bambuddy's default for both is null — there is no value to show.
BAMBUDDY_DEFAULTS = PrintOptions(
    bed_levelling="auto",
    flow_cali="auto",
    vibration_cali=True,
    nozzle_offset_cali="auto",
    layer_inspect=False,
    timelapse=False,
    use_ams=True,
    quantity=1,
    manual_start=False,
    insert_at_top=False,
    auto_off_after=False,
    preheat_override="inherit",
)


def resolve(*scopes: PrintOptions | None) -> PrintOptions:
    """Merge overlays field by field, later scopes winning.

    An unset field never clears a value an earlier scope set, which is what makes
    "remember timelapse off for this printer" survive a per-model override of something
    else entirely.
    """
    merged: dict[str, Any] = {}
    for scope in scopes:
        if scope is not None:
            merged.update(scope.queue_fields())
    return PrintOptions.model_validate(merged)
