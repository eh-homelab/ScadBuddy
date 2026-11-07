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

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.models import CalibrationMode, PreheatOverride
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN

#: The scopes an override can be remembered at, least to most specific. ``request`` is
#: not one of them — it is not remembered. ``model`` is the print's own subject's layer,
#: keyed by its :func:`options_scope`.
OptionScope = Literal["global", "printer", "model"]

_MODEL_ID = re.compile(MODEL_ID_PATTERN)
#: As ``PrintSubject.library`` writes it: no leading zero, so one file has one key.
_LIBRARY_SCOPE = re.compile(r"library:[1-9][0-9]*")


def options_scope(subject: PrintSubject, slug: str | None) -> str | None:
    """What a print's own options and remembered choices are kept under (#1754): an
    output's model id, which every output of the model shares, or a library file's
    subject key ``library:<file id>``. Both live in the one store, the per-model maps
    (``model_print_options``, ``model_print_choices``); a model id is a slug or
    ``builtin:<slug>``, never ``library:``, so the two kinds never meet. ``None`` for
    an output whose model is not known."""
    return slug if subject.kind == "output" else subject.key


def library_options_scope(file_id: int) -> str:
    return PrintSubject.library(file_id).key


def is_options_scope(key: str) -> bool:
    """Whether ``key`` is one :func:`options_scope` could return."""
    return (
        len(key) <= MAX_MODEL_ID_LENGTH and _MODEL_ID.fullmatch(key) is not None
    ) or _LIBRARY_SCOPE.fullmatch(key) is not None


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
    #: 1-1000 is **ScadBuddy's** bound, mirroring ``PrintRunRequest.copies``, which is the
    #: other control for this same value. Bambuddy declares ``quantity`` as a plain
    #: integer with no bound at all.
    quantity: int | None = Field(default=None, ge=1, le=1000)
    manual_start: bool | None = None
    insert_at_top: bool | None = None
    auto_off_after: bool | None = None
    project_id: int | None = None
    preheat_override: PreheatOverride | None = None
    #: 0-65 is **Bambuddy's** own bound, recorded in
    #: ``tests/bambuddy/recordings/openapi/scadbuddy-routes.json`` and asserted against
    #: that recording by ``test_the_measured_bound_comes_from_bambuddys_own_schema``.
    #: Mirrored so a bad value 422s here rather than at Bambuddy.
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


#: What a print over the LAN does with each calibration (#5352), as the one value of
#: Bambuddy's option that means the same thing; ``None`` where no value does.
#: `printers/lan.py` `project_file_command` (line 173) sends ``flow_cali`` from
#: `lan_models.ProjectFile.flow_cali` (line 35, default ``False``), which
#: `printers/print_activities.py` `_command` never sets: always ``false``, so ``"off"``.
#: It sends no field for bed levelling, nozzle offset or vibration, so the printer applies
#: its own default (`.claude/skills/h2c-print-from-scadbuddy/references/lan-path.md`).
#: That is ``"auto"`` for the two three-way options, whose ``auto`` leaves the stage to
#: the printer. ``vibration_cali`` is a plain bool with no "the printer decides", so
#: neither value is what the LAN does.
LAN_CALIBRATIONS: dict[str, str | None] = {
    "bed_levelling": "auto",
    "flow_cali": "off",
    "nozzle_offset_cali": "auto",
    "vibration_cali": None,
}


#: The problem ``type`` of :func:`lan_calibration_refusal`'s 422 (#5602), so the print
#: route can tell it from the run's other refusals and word it for its caller.
LAN_CALIBRATION_PROBLEM = "https://scadbuddy.dev/problems/print/lan-calibration"

#: The sentence the print route adds to :func:`lan_calibration_refusal`'s text for a
#: caller of the API or the agent (#5602, `api/printing.py` `_answered`): the request's
#: own ``path`` picks Bambuddy's queue for one print. The print dialog's text names no
#: ``path``, which the dialog does not send.
LAN_CALIBRATION_API_HINT = (
    ' Or send path "bambuddy" in the request to print this one print through Bambuddy\'s queue.'
)


def lan_calibration_refusal(options: PrintOptions) -> str | None:
    """The 422 for a print's own ``options`` that ask a run over the LAN for a
    calibration it cannot honour (#5352), else ``None``: refused, never dropped without
    a word. The value the LAN start sends (`LAN_CALIBRATIONS`) passes. Only the request's
    own options are judged; a remembered one is Bambuddy's queue option, as before.
    Its 422 carries ``LAN_CALIBRATION_PROBLEM``."""
    asked = [
        f"{name} {_shown(getattr(options, name))}"
        for name, honoured in LAN_CALIBRATIONS.items()
        if getattr(options, name) is not None and getattr(options, name) != honoured
    ]
    if not asked:
        return None
    return (
        f"A print over the LAN cannot set {', '.join(asked)}: it sends flow calibration off"
        " and leaves bed levelling, nozzle offset and vibration calibration to the printer."
        " Leave them at the default, or print through Bambuddy's queue: in Settings > Printers,"
        ' set how the printer prints to "Through Bambuddy" (it must link a Bambuddy printer).'
    )


def _shown(value: object) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value).lower()
