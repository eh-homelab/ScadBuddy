"""What `test_rack_live` counts as this job's, from the printer's reports (#4933).

The live run of 2026-10-29 passed on the previous job's values: the printer's report
named the probe's ``subtask_name`` while ``layer_num`` (54) and ``device.nozzle.tar_id``
(20, the hotend already on the toolhead) were still the last job's, and it reported
``RUNNING`` within 2 s of the start (as Task 7 measured, `test_lan_live`'s docstring),
long before a rack fetch or layer 1. So nothing is taken from one report alone. A
`RackProbe` compares each report with the one read before the command (the baseline)
and counts only a change it saw after the printer took this job:

- the job is ours from the first report that names its ``subtask_name`` (unique per
  run, so no earlier job can carry it) in ``PREPARE``, ``SLICING`` or ``RUNNING``; the
  baseline is idle (`idle_problem`), so such a state is this job's;
- a rack fetch is a ``tar_id`` naming a rack hotend (`RACK_IDS`) that differs from the
  last one seen, the baseline's first; before the pick's first fetch, the baseline's
  own hotend (when the pick does not send it) is that hotend parked, not a fetch;
- layer 1 counts only after the job reported a ``layer_num`` reset: 0, or lower than
  the baseline's.

A fetch proves the mapping only when the printer had a choice (#5005). The run of
2026-10-30 sent every rack hotend that fit the plate (16 to 20), and the H2C fetched
each in turn (16, 17, 18, 19, 20, 16) and was still at layer 0 after 20 min: a printer
that ignored the mapping could have fetched the same. So the probe also takes
``excluded``, the rack hotends that fit one of the plate's rack groups but that the pick
does not send (`excluded_hotends`), and the test refuses a pick with none
(`proof_problem`). A fetch of an excluded hotend fails at once.

It ends (`done`) once the job has fetched every rack hotend the mapping sends (except
the one mounted at the baseline, which may never leave the toolhead) and no other: the
pre-print sequence. It needs no layer 1, which a plate of several rack groups reaches
only after 20 min or more. With ``layer1`` it also waits for the job's layer 1, so a
fetch later in the pre-print is watched too. It fails on a fetch the mapping does not
name, a ``print_error`` other than the baseline's, or the job pausing (``PAUSE``) or
ending (``FAILED``/``FINISH``) before that.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from scadbuddy.bambuddy.models import NozzleRackSlot
from scadbuddy.printers.nozzle_mapping import RACK_POSITION_BASE, RACK_POSITIONS
from scadbuddy.rack.rank import RackGroup, eligible, rack_positions

#: The rack hotends' physical ids (16 to 21); ``tar_id`` holds others too.
RACK_IDS = frozenset(RACK_POSITION_BASE + p for p in RACK_POSITIONS)

#: The states a printer may start a print from. ``FINISH`` and ``FAILED`` leave the last
#: job's plate where it was: the printer reports no empty-bed flag, so an empty bed is
#: the runner's word (``SCADBUDDY_TEST_PRINTER_PRINT=1``), never read from the report.
IDLE_STATES = frozenset({"IDLE", "FINISH", "FAILED"})
STARTED_STATES = frozenset({"PREPARE", "SLICING", "RUNNING"})


class ProbeError(AssertionError):
    pass


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def tar_id(report: dict[str, Any]) -> int | None:
    nozzle = (report.get("device") or {}).get("nozzle") or {}
    return _int(nozzle.get("tar_id"))


def fetch_problem(baseline: dict[str, Any], sent: frozenset[int]) -> str | None:
    """Why the pick can show no fetch, or ``None``: every rack hotend it sends is the one
    already on the toolhead, so ``tar_id`` never changes and `RackProbe` never ends."""
    mounted = tar_id(baseline)
    if not {n for n in sent if n in RACK_IDS} - {mounted}:
        return f"the pick {sorted(sent)} sends no rack hotend but the mounted {mounted}"
    return None


def skip_positions(rack: Sequence[NozzleRackSlot], skip: Collection[int]) -> list[NozzleRackSlot]:
    """``rack`` with the hotends at rack positions ``skip`` (1-6) given no diameter, so
    no group fits them and the pick leaves them out. Each keeps its id, so the carriage's
    hotend still fills its own position (`rank.rack_positions`)."""
    hidden = {id(entry) for position, entry in rack_positions(rack).items() if position in skip}
    return [
        entry.model_copy(update={"nozzle_diameter": ""}) if id(entry) in hidden else entry
        for entry in rack
    ]


def excluded_hotends(
    rack: Sequence[NozzleRackSlot], groups: Sequence[RackGroup], sent: frozenset[int]
) -> frozenset[int]:
    """The rack hotends (`RACK_IDS`) that fit one of ``groups`` but are not ``sent``: the
    ones a printer that ignored the mapping could have fetched instead. ``rack`` is the
    printer's own, before `skip_positions`: a skipped hotend still fits. Only a hotend
    with a serial counts: an empty mount reads ``N/A`` (`NozzleRackSlot.serial_number`)
    and may still report a diameter (`fixtures/lan-report-h2c.json`, id 17)."""
    return frozenset(
        RACK_POSITION_BASE + position
        for position, entry in rack_positions(rack).items()
        if RACK_POSITION_BASE + position not in sent
        and entry.serial_number
        and any(eligible(entry, g.nozzle_diameter, g.volume_type) for g in groups)
    )


def proof_problem(sent: frozenset[int], excluded: frozenset[int]) -> str | None:
    """Why the fetches of this pick cannot prove the mapping, or ``None``: it sends every
    rack hotend that fits the plate, so any order of fetches is one the mapping names."""
    if excluded:
        return None
    return (
        f"the pick sends {sorted(n for n in sent if n in RACK_IDS)}, every rack hotend "
        "that fits the plate, so no fetch can show the printer obeyed it: use a plate of "
        "fewer rack groups, or leave a position out with SCADBUDDY_TEST_PRINTER_RACK_SKIP"
    )


def idle_problem(baseline: dict[str, Any], name: str) -> str | None:
    """Why the printer must not be started now, or ``None``: it is printing (or paused),
    the probe's name is already on it, or it holds an error outside a failed job."""
    state = baseline.get("gcode_state")
    if state not in IDLE_STATES:
        return f"the printer is {state!r}, not one of {sorted(IDLE_STATES)}"
    if baseline.get("subtask_name") == name:
        return f"the printer already names {name!r}"
    error = _int(baseline.get("print_error")) or 0
    if error and state != "FAILED":
        return f"the printer reports print_error {error} in {state}; clear it on the screen"
    return None


@dataclass
class RackProbe:
    name: str
    baseline: dict[str, Any]
    sent: frozenset[int]
    #: Rack hotends that fit the plate but are not sent (`excluded_hotends`).
    excluded: frozenset[int] = frozenset()
    #: Also wait for the job's layer 1, not only the pre-print fetches.
    layer1: bool = False
    #: A report named the job: the printer took it, so it is the probe's to stop.
    taken: bool = False
    started: bool = False
    #: The job's last ``gcode_state``, once started.
    state: str | None = None
    reset: bool = False
    layer: int | None = None
    fetched: list[int] = field(default_factory=list)
    _last_tar: int | None = None

    def __post_init__(self) -> None:
        self._last_tar = self._mounted = tar_id(self.baseline)
        self._baseline_layer = _int(self.baseline.get("layer_num"))
        self._baseline_error = _int(self.baseline.get("print_error")) or 0

    @property
    def wanted(self) -> frozenset[int]:
        """The rack hotends the job must fetch: every one sent but the mounted one."""
        return frozenset(n for n in self.sent if n in RACK_IDS) - {self._mounted}

    @property
    def done(self) -> bool:
        if not (self.started and self.excluded and self.wanted <= set(self.fetched)):
            return False
        return not self.layer1 or (self.reset and (self.layer or 0) >= 1)

    def feed(self, report: dict[str, Any]) -> list[str]:
        """Take one report; return what it showed (for ``-s``). Raises `ProbeError`."""
        if report.get("command") == "project_file" and report.get("result") == "FAIL":
            raise ProbeError(f"the printer refused the start: {report.get('reason')!r}")
        if report.get("subtask_name") != self.name:
            return []
        self.taken = True
        state = report.get("gcode_state")
        error = _int(report.get("print_error")) or 0
        if not self.started:
            if state not in STARTED_STATES:
                # The name, over the last job's state: not this job's yet.
                return []
            self.started = True
        shown: list[str] = []
        self.state = state
        if state == "PAUSE":
            # A runout or an HMS pause waits for someone at the printer: never a pass.
            raise ProbeError(f"the job paused before it was verified: {self.missing()}")
        if state in ("FAILED", "FINISH"):
            raise ProbeError(
                f"the job ended ({state}, print_error {error}) before it was verified: "
                f"{self.missing()}"
            )
        if error and error != self._baseline_error:
            raise ProbeError(f"print_error {error} while {state}")
        target = tar_id(report)
        if target is not None and target != self._last_tar:
            self._last_tar = target
            if target == self._mounted and target not in self.sent and not self.fetched:
                # Before the pick's first fetch, the baseline's own hotend is that hotend
                # handled (parked), not fetched; after it, the hotend is fetched again.
                shown.append(f"parking rack hotend {target}")
            elif target in RACK_IDS:
                self.fetched.append(target)
                shown.append(f"fetching rack hotend {target}")
                if target in self.excluded:
                    raise ProbeError(
                        f"fetched {target}, which the mapping leaves out: sent "
                        f"{sorted(self.sent)}, excluded {sorted(self.excluded)}"
                    )
                if target not in self.sent:
                    raise ProbeError(f"fetched {target}, sent {sorted(self.sent)}")
        layer = _int(report.get("layer_num"))
        if layer is not None:
            if layer == 0 or (self._baseline_layer is not None and layer < self._baseline_layer):
                self.reset = True
            if self.reset and state == "RUNNING":
                if layer != self.layer and layer >= 1:
                    shown.append(f"layer {layer}")
                self.layer = layer
        return shown

    def missing(self) -> str:
        """What the probe has not seen yet, for a timeout's message."""
        if not self.started:
            seen = (
                f"reports named {self.name!r} only over the last job's state"
                if self.taken
                else f"no report named {self.name!r}"
            )
            return f"{seen}, never in {sorted(STARTED_STATES)}"
        gaps = []
        if not self.excluded:
            gaps.append("no rack hotend left out, so no fetch proves the mapping")
        if not self.fetched:
            gaps.append(f"no rack fetch (tar_id stayed {self._last_tar})")
        elif unfetched := self.wanted - set(self.fetched):
            gaps.append(f"not yet fetched {sorted(unfetched)} (fetched {self.fetched})")
        if self.layer1:
            if not self.reset:
                gaps.append(f"no layer_num reset (baseline {self._baseline_layer})")
            elif (self.layer or 0) < 1:
                gaps.append("not yet RUNNING layer 1")
        return f"{'; '.join(gaps) or 'nothing'} (last {self.state})"
