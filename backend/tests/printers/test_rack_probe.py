"""`rack_probe`, offline, on report sequences shaped like the H2C's (#4933)."""

from __future__ import annotations

from typing import Any

import pytest

from scadbuddy.printers.lan import merge_report
from tests.printers.rack_probe import ProbeError, RackProbe, idle_problem

NAME = "scadbuddy-rack-probe-1"
SENT = frozenset({16, 17, 18, 19, 20})


def report(
    state: str,
    *,
    name: str = NAME,
    layer: int = 0,
    tar: int = 20,
    error: int = 0,
) -> dict[str, Any]:
    return {
        "command": "push_status",
        "subtask_name": name,
        "gcode_state": state,
        "layer_num": layer,
        "print_error": error,
        "device": {"nozzle": {"src_id": tar, "tar_id": tar}},
    }


#: The printer before the 2026-10-29 run: the last job finished at layer 54 with
#: hotend 20 on the toolhead (#4933).
BASELINE = report("FINISH", name="cap-keychain", layer=54, tar=20)


def probe(baseline: dict[str, Any] = BASELINE, sent: frozenset[int] = SENT) -> RackProbe:
    return RackProbe(name=NAME, baseline=baseline, sent=sent)


def run(probe: RackProbe, reports: list[dict[str, Any]]) -> list[str]:
    shown: list[str] = []
    for one in reports:
        shown += probe.feed(one)
        if probe.done:
            break
    return shown


def test_the_stale_sequence_of_the_first_live_run_is_not_a_pass() -> None:
    # What passed on 2026-10-29: the job's name over the last job's tar_id and layer_num,
    # RUNNING within 2 s, then the test's own stop.
    watch = probe()
    shown = run(watch, [report("RUNNING", layer=54, tar=20), report("RUNNING", layer=54, tar=20)])
    assert not watch.done
    assert watch.fetched == [] and shown == []
    assert "no rack fetch" in watch.missing() and "no layer_num reset" in watch.missing()
    with pytest.raises(ProbeError, match="ended"):
        watch.feed(report("FAILED", layer=0, tar=20, error=50348044))


def test_the_stale_sequence_as_merged_deltas_is_not_a_pass() -> None:
    # The same as partial deltas over the pushall, as `LanPrinter.reports` merges them.
    merged = merge_report(None, BASELINE)
    watch = probe(merged)
    for delta in ({"subtask_name": NAME}, {"gcode_state": "RUNNING"}):
        merged = merge_report(merged, delta)
        watch.feed(merged)
    assert watch.started and not watch.done and watch.fetched == []


def test_the_name_over_the_last_jobs_finish_is_not_a_start() -> None:
    watch = probe()
    watch.feed(report("FINISH", layer=54))
    assert watch.taken and not watch.started


def test_a_real_start_passes_after_a_fetch_and_layer_1() -> None:
    watch = probe()
    shown = run(
        watch,
        [
            report("PREPARE", layer=54, tar=20),  # the name first, the layer still stale
            report("PREPARE", layer=0, tar=20),
            report("PREPARE", layer=0, tar=16),
            report("RUNNING", layer=0, tar=16),
            report("RUNNING", layer=1, tar=16),
        ],
    )
    assert watch.done
    assert watch.fetched == [16]
    assert shown == ["fetching rack hotend 16", "layer 1"]


def test_layer_1_without_a_fetch_keeps_waiting() -> None:
    watch = probe()
    run(watch, [report("PREPARE", layer=0), report("RUNNING", layer=1), report("RUNNING", layer=2)])
    assert not watch.done
    assert watch.missing() == "no rack fetch (tar_id stayed 20)"


def test_a_layer_above_the_baseline_is_no_reset() -> None:
    watch = probe(report("FINISH", name="old", layer=3, tar=19))
    run(watch, [report("RUNNING", layer=5, tar=16)])
    assert watch.fetched == [16] and not watch.reset and not watch.done


def test_a_fetch_the_mapping_does_not_name_fails() -> None:
    watch = probe(sent=frozenset({16}))
    with pytest.raises(ProbeError, match="fetched 21"):
        run(watch, [report("PREPARE", layer=0, tar=21)])


def test_the_baselines_error_is_stale_but_a_new_one_fails() -> None:
    watch = probe(report("FAILED", name="old", layer=0, tar=20, error=50348044))
    watch.feed(report("PREPARE", layer=0, error=50348044))
    with pytest.raises(ProbeError, match="print_error 7"):
        watch.feed(report("PREPARE", layer=0, error=7))


def test_a_refused_start_fails() -> None:
    with pytest.raises(ProbeError, match="refused"):
        probe().feed({"command": "project_file", "result": "FAIL", "reason": "busy"})


def test_another_jobs_reports_are_ignored() -> None:
    watch = probe()
    assert watch.feed(report("RUNNING", name="someone-else", layer=1, tar=17)) == []
    assert not watch.taken and watch.fetched == []


@pytest.mark.parametrize(
    ("baseline", "problem"),
    [
        (report("FINISH", name="old"), None),
        (report("IDLE", name=""), None),
        (report("FAILED", name="old", error=50348044), None),
        (report("RUNNING", name="old"), "'RUNNING'"),
        (report("PAUSE", name="old"), "'PAUSE'"),
        (report("FINISH"), "already names"),
        (report("FINISH", name="old", error=7), "print_error 7"),
    ],
)
def test_idle_problem(baseline: dict[str, Any], problem: str | None) -> None:
    found = idle_problem(baseline, NAME)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found
