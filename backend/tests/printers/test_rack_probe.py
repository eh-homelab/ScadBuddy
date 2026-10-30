"""`rack_probe`, offline, on report sequences shaped like the H2C's (#4933)."""

from __future__ import annotations

from typing import Any

import pytest

from scadbuddy.bambuddy.models import NozzleRackSlot
from scadbuddy.printers.lan import merge_report
from scadbuddy.rack.rank import RackGroup, rack_positions
from tests.printers.rack_probe import (
    ProbeError,
    RackProbe,
    excluded_hotends,
    fetch_problem,
    idle_problem,
    proof_problem,
    skip_positions,
)

NAME = "scadbuddy-rack-probe-1"
#: A pick of two rack hotends; the other fitting ones (20 mounted) are left out.
SENT = frozenset({16, 17})
EXCLUDED = frozenset({18, 19, 20})


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


def probe(
    baseline: dict[str, Any] = BASELINE,
    sent: frozenset[int] = SENT,
    excluded: frozenset[int] = EXCLUDED,
    *,
    layer1: bool = False,
) -> RackProbe:
    return RackProbe(name=NAME, baseline=baseline, sent=sent, excluded=excluded, layer1=layer1)


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
    watch = probe(layer1=True)
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
    assert "only over the last job's state" in watch.missing()


def test_a_pick_of_only_the_mounted_hotend_is_refused() -> None:
    # tar_id would never change, so the probe could never end (#4936 review).
    assert fetch_problem(BASELINE, frozenset({20})) is not None
    assert fetch_problem(BASELINE, frozenset({20, 16})) is None


#: A start that fetches the two hotends sent, then prints.
REAL_START = [
    report("PREPARE", layer=54, tar=20),  # the name first, the layer still stale
    report("PREPARE", layer=0, tar=20),
    report("PREPARE", layer=0, tar=16),
    report("PREPARE", layer=0, tar=17),
    report("RUNNING", layer=0, tar=17),
    report("RUNNING", layer=1, tar=17),
]


def test_a_real_start_passes_once_every_hotend_sent_is_fetched() -> None:
    # The pre-print sequence is the verdict: no wait for layer 1 (#5005).
    watch = probe()
    shown = run(watch, REAL_START)
    assert watch.done
    assert watch.fetched == [16, 17] and watch.layer is None
    assert shown == ["fetching rack hotend 16", "fetching rack hotend 17"]


def test_with_layer1_a_real_start_passes_at_layer_1() -> None:
    watch = probe(layer1=True)
    shown = run(watch, REAL_START[:5])
    assert not watch.done and watch.missing() == "not yet RUNNING layer 1 (last RUNNING)"
    shown += run(watch, REAL_START[5:])
    assert watch.done
    assert shown == ["fetching rack hotend 16", "fetching rack hotend 17", "layer 1"]


def test_one_hotend_fetched_of_two_sent_keeps_waiting() -> None:
    watch = probe()
    run(watch, REAL_START[:3])
    assert not watch.done
    assert watch.missing() == "not yet fetched [17] (fetched [16]) (last PREPARE)"


def test_the_mounted_hotend_sent_need_not_be_fetched() -> None:
    # 20 is on the toolhead at the baseline: tar_id may never change to it.
    watch = probe(sent=frozenset({16, 20}), excluded=frozenset({17, 18, 19}))
    run(watch, [report("PREPARE", layer=0, tar=16)])
    assert watch.done


def test_a_fetch_of_a_hotend_left_out_fails_at_once() -> None:
    watch = probe()
    with pytest.raises(ProbeError, match="fetched 18, which the mapping leaves out"):
        run(watch, [report("PREPARE", layer=0, tar=16), report("PREPARE", layer=0, tar=18)])


#: The H2C's rack on 2026-10-30 (#5005): 0.4 mm hotends at positions 1 to 5, position 6
#: empty. The serials are made up.
RACK_2026_10_30 = [
    NozzleRackSlot(id=0, nozzle_type="HS01", nozzle_diameter="0.4", serial_number="S20"),
    NozzleRackSlot(id=1, nozzle_type="HS01", nozzle_diameter="0.4", serial_number="SF"),
    *(
        NozzleRackSlot(id=n, nozzle_type="HS01", nozzle_diameter="0.4", serial_number=f"S{n}")
        for n in (16, 17, 18, 19)
    ),
    NozzleRackSlot(id=21, nozzle_type="HS01", nozzle_diameter="0.4", serial_number="N/A"),
]
#: The cap-keychain plate's five rack groups (slot groups ``[1, 0, 2, 3, 4, 5]``).
GROUPS_2026_10_30 = [
    RackGroup(group_id=g, nozzle_diameter="0.4", volume_type="Standard") for g in (1, 2, 3, 4, 5)
]
#: What the probe saw after the start on 2026-10-30: each rack hotend in turn, then 16
#: again, then ``RUNNING`` at layer 0 for the rest of the 20 min.
SEQUENCE_2026_10_30 = [
    report("PREPARE", layer=0, tar=20),
    *(report("PREPARE", layer=0, tar=n) for n in (16, 17, 18, 19, 20, 16)),
    *(report("RUNNING", layer=0, tar=16) for _ in range(20)),
]


def test_the_2026_10_30_pick_is_refused_as_no_proof() -> None:
    # Its mapping sent every rack hotend that fit the plate, so the run proves nothing.
    sent = frozenset({16, 17, 18, 19, 20})
    excluded = excluded_hotends(RACK_2026_10_30, GROUPS_2026_10_30, sent)
    assert excluded == frozenset()  # 21 is an empty mount
    problem = proof_problem(sent, excluded)
    assert problem is not None and "every rack hotend that fits the plate" in problem


def test_the_2026_10_30_sequence_is_no_pass_for_its_mapping() -> None:
    before = report("FAILED", name="cap-keychain", layer=0, tar=20)
    watch = RackProbe(name=NAME, baseline=before, sent=frozenset({16, 17, 18, 19, 20}))
    shown = run(watch, SEQUENCE_2026_10_30)
    assert watch.fetched == [16, 17, 18, 19, 20, 16]
    assert shown == [f"fetching rack hotend {n}" for n in (16, 17, 18, 19, 20, 16)]
    assert not watch.done
    assert (
        watch.missing() == "no rack hotend left out, so no fetch proves the mapping (last RUNNING)"
    )


def test_the_2026_10_30_sequence_fails_fast_for_a_mapping_that_leaves_17_out() -> None:
    # Had the pick sent 16 and 18 and left 17 out, the fetch of 17 fails the run at
    # once, minutes before the timeout, instead of waiting for a layer 1 that never came.
    before = report("FAILED", name="cap-keychain", layer=0, tar=20)
    watch = RackProbe(
        name=NAME,
        baseline=before,
        sent=frozenset({16, 18}),
        excluded=frozenset({17, 19, 20}),
    )
    with pytest.raises(ProbeError, match="fetched 17, which the mapping leaves out"):
        run(watch, SEQUENCE_2026_10_30)
    assert watch.fetched == [16, 17]


def test_the_2026_10_30_sequence_passes_at_its_fetches_for_a_mapping_that_obeys() -> None:
    # Sent 16 to 18 with 19 left out: the probe ends at the fetch of 18, at layer 0.
    before = report("FAILED", name="cap-keychain", layer=0, tar=20)
    watch = RackProbe(
        name=NAME,
        baseline=before,
        sent=frozenset({16, 17, 18}),
        excluded=frozenset({19, 20}),
    )
    run(watch, SEQUENCE_2026_10_30)
    assert watch.done and watch.fetched == [16, 17, 18]


def test_skip_positions_leaves_the_hotend_out_of_the_pick_but_not_out_of_the_rack() -> None:
    skipped = skip_positions(RACK_2026_10_30, {2, 5})
    positions = rack_positions(skipped)
    # Position 5's hotend is the carriage's (id 0): it keeps its place, unfit for a group.
    assert positions[5].id == 0 and positions[5].nozzle_diameter == ""
    assert positions[2].id == 17 and positions[2].nozzle_diameter == ""
    assert positions[1].nozzle_diameter == "0.4"
    # The hotends skipped still fit the plate: a fetch of one is left out of the mapping.
    sent = frozenset({16, 18, 19})
    assert excluded_hotends(RACK_2026_10_30, GROUPS_2026_10_30, sent) == {17, 20}


def test_a_hotend_that_fits_no_group_is_not_left_out() -> None:
    wide = [
        NozzleRackSlot(id=16, nozzle_type="HS01", nozzle_diameter="0.4", serial_number="A"),
        NozzleRackSlot(id=17, nozzle_type="HS01", nozzle_diameter="0.6", serial_number="B"),
        NozzleRackSlot(id=18, nozzle_type="HH01", nozzle_diameter="0.4", serial_number="C"),
    ]
    groups = [RackGroup(group_id=1, nozzle_diameter="0.4", volume_type="Standard")]
    assert excluded_hotends(wide, groups, frozenset({16})) == frozenset()


def test_layer_1_without_a_fetch_keeps_waiting() -> None:
    watch = probe()
    run(watch, [report("PREPARE", layer=0), report("RUNNING", layer=1), report("RUNNING", layer=2)])
    assert not watch.done
    assert watch.missing() == "no rack fetch (tar_id stayed 20) (last RUNNING)"


def test_a_layer_above_the_baseline_is_no_reset() -> None:
    watch = probe(report("FINISH", name="old", layer=3, tar=19))
    run(watch, [report("RUNNING", layer=5, tar=16)])
    assert watch.fetched == [16] and not watch.reset and not watch.done


def test_the_mounted_hotend_parked_is_no_fetch() -> None:
    # tar_id back on the baseline's hotend before the pick's first fetch (#4936 review).
    watch = probe(report("FINISH", name="old", layer=54, tar=19), sent=frozenset({16, 17}))
    shown = run(
        watch,
        [
            report("PREPARE", layer=0, tar=255),
            report("PREPARE", layer=0, tar=19),
            report("PREPARE", layer=0, tar=16),
        ],
    )
    assert watch.fetched == [16]
    assert shown == ["parking rack hotend 19", "fetching rack hotend 16"]
    with pytest.raises(ProbeError, match="fetched 19"):
        watch.feed(report("RUNNING", layer=0, tar=19))


def test_a_pause_fails() -> None:
    watch = probe()
    watch.feed(report("RUNNING", layer=0, tar=16))
    with pytest.raises(ProbeError, match="paused"):
        watch.feed(report("PAUSE", layer=0, tar=16))


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
