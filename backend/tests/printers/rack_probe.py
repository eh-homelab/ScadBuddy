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
- a rack fetch is a ``tar_id`` (16 or more) that differs from the last one seen, the
  baseline's first;
- layer 1 counts only after the job reported a ``layer_num`` reset: 0, or lower than
  the baseline's.

It ends (`done`) once the job is past layer 1 with at least one rack fetch seen, and
fails on a fetch the mapping does not name, a ``print_error`` other than the
baseline's, or the job ending (``FAILED``/``FINISH``) before that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scadbuddy.printers.status import RACK_FIRST_ID

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
    if not {n for n in sent if n >= RACK_FIRST_ID} - {mounted}:
        return f"the pick {sorted(sent)} sends no rack hotend but the mounted {mounted}"
    return None


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
    #: A report named the job: the printer took it, so it is the probe's to stop.
    taken: bool = False
    started: bool = False
    reset: bool = False
    layer: int | None = None
    fetched: list[int] = field(default_factory=list)
    _last_tar: int | None = None

    def __post_init__(self) -> None:
        self._last_tar = tar_id(self.baseline)
        self._baseline_layer = _int(self.baseline.get("layer_num"))
        self._baseline_error = _int(self.baseline.get("print_error")) or 0

    @property
    def done(self) -> bool:
        return self.started and self.reset and (self.layer or 0) >= 1 and bool(self.fetched)

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
            if target >= RACK_FIRST_ID:
                self.fetched.append(target)
                shown.append(f"fetching rack hotend {target}")
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
        if not self.fetched:
            gaps.append(f"no rack fetch (tar_id stayed {self._last_tar})")
        if not self.reset:
            gaps.append(f"no layer_num reset (baseline {self._baseline_layer})")
        elif (self.layer or 0) < 1:
            gaps.append("not yet RUNNING layer 1")
        return "; ".join(gaps) or "nothing"
