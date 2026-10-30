"""A rack print's ``nozzle_mapping`` against a real H2C (hot-path plan 2026-10-18 Task 8).
Skipped unless `tests.conftest.PRINTER_ENV` names a printer, and it *starts a print*, so
it runs only with ``SCADBUDDY_TEST_PRINTER_PRINT=1`` and an empty bed; run by hand with
the user at the printer, never in CI.

What it rests on was measured without it: the wire Bambuddy sent this printer for prints
that completed (`printers/nozzle_mapping.py`'s docstring,
`fixtures/rack-project-file.json`). This checks ScadBuddy's own pick end to end: the plate
``SCADBUDDY_TEST_PRINTER_FILE`` names (a sliced rack plate the printer already holds,
e.g. ``/cache/x.gcode.3mf``), its rack plan read from the file, the rack ranked from the
printer's own report, started with ``SCADBUDDY_TEST_PRINTER_AMS`` (comma-separated tray
ids, one per filament slot) as its ``ams_mapping`` and the pick as its ``nozzle_mapping``.
What counts as this job's is `tests.printers.rack_probe` (#4933: the first live run
passed on the previous job's ``tar_id`` and ``layer_num``).

The pick must leave out at least one rack hotend that fits the plate (#5005): the run
of 2026-10-30 sent all five rack hotends to a plate of five rack groups, so the fetches
it saw could not tell a printer that obeyed the mapping from one that did not. It
refuses such a pick before the upload (`rack_probe.proof_problem`), and any fetch
(``device.nozzle.tar_id``) of a left-out hotend fails the run at once. So take a plate
of fewer rack groups than the rack has fitting hotends: one or two rack groups is the
cheapest, since the H2C's pre-print fetches each mapped hotend in turn (the 2026-10-30
plate's five took about 19 min, still at layer 0). Bambuddy's ``print_queue`` items 374
(one filament, ``nozzle_mapping [18]``) and 367 (two rack groups, ``[18, 1, 1, 20, …]``)
were such plates (`fixtures/rack-project-file.json`). Or leave rack positions (1-6) out
of the pick with ``SCADBUDDY_TEST_PRINTER_RACK_SKIP`` (comma-separated).

A hotend left out shows only that the printer *had* a choice. A pick that is also what
the printer would choose with no ``nozzle_mapping`` passes without proving anything,
and the test cannot read that choice. So move the pick off the likely default: run
``_DRY=1`` once with no skip, then skip the positions it picked (and the mounted
hotend's, if it fits). The pick then sends hotends that neither the ranking nor a
printer that keeps the mounted hotend would take first.

It passes once the job has fetched every rack hotend the mapping sends (the mounted one
excepted) and none other, then stops the print: the pre-print sequence is the verdict, so
it never waits for layer 1. ``SCADBUDDY_TEST_PRINTER_LAYER1=1`` also waits for the job's
layer 1, watching the whole pre-print for a fetch the mapping leaves out (allow 30 min or
more for that). It waits ``SCADBUDDY_TEST_PRINTER_WAIT`` seconds (default `START_WAIT`),
failing on a ``print_error`` or on a fetch the mapping does not name.

``SCADBUDDY_TEST_PRINTER_DRY=1`` (in place of ``_PRINT``) does everything before the
upload, reads the plate and the rack and prints the pick and the left-out hotends, then
skips: no print is started, so a runner can try files for one that proves the mapping.

It refuses to start unless the printer is idle (`rack_probe.idle_problem`): ``IDLE``,
``FINISH`` or ``FAILED``, and no ``print_error`` outside a failed job, and a pick that
sends a rack hotend other than the mounted one (`rack_probe.fetch_problem`). The
printer reports no empty-bed flag, so ``SCADBUDDY_TEST_PRINTER_PRINT=1`` is the runner's
word that the bed is clear. Whoever runs it watches the screen: the hotends it picks up
are the ones printed (``-s`` shows the mapping sent, each fetch and each layer).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from io import BytesIO

import pytest

from scadbuddy.printers.lan import (
    UPLOAD_DIR,
    LanPrinter,
    fetch_cert_sha256,
    file_url,
    project_file_command,
)
from scadbuddy.printers.lan_models import ProjectFile
from scadbuddy.printers.nozzle_mapping import FIXED_NOZZLE_ID, choose_nozzles, read_rack_plan
from scadbuddy.printers.status import status_from_report
from scadbuddy.rack.rank import rack_groups
from tests.conftest import printer_env
from tests.printers.rack_probe import (
    RackProbe,
    excluded_hotends,
    fetch_problem,
    idle_problem,
    proof_problem,
    skip_positions,
)
from tests.printers.test_status import row

pytestmark = pytest.mark.requires_printer

#: How long the printer may take by default to heat and fetch the mapped rack hotends.
START_WAIT = 1800

# Read at import, as `tests.conftest` reads the printer's: the API tests scrub them.
PRINT = os.environ.get("SCADBUDDY_TEST_PRINTER_PRINT") == "1"
DRY = os.environ.get("SCADBUDDY_TEST_PRINTER_DRY") == "1"
FILE = os.environ.get("SCADBUDDY_TEST_PRINTER_FILE", "")
AMS = os.environ.get("SCADBUDDY_TEST_PRINTER_AMS", "")
SKIP = os.environ.get("SCADBUDDY_TEST_PRINTER_RACK_SKIP", "")
LAYER1 = os.environ.get("SCADBUDDY_TEST_PRINTER_LAYER1") == "1"
# Parsed in the test, so a bad value fails only the run that uses it, not collection.
WAIT = os.environ.get("SCADBUDDY_TEST_PRINTER_WAIT", "")


@pytest.fixture
def printer() -> dict[str, str]:
    env = printer_env()
    assert env is not None
    return env


@pytest.mark.skipif(
    not (PRINT or DRY) or not FILE or not (AMS or DRY),
    reason="starts a print: opt in with SCADBUDDY_TEST_PRINTER_PRINT=1 (or _DRY=1), _FILE and _AMS",
)
async def test_a_rack_print_uses_the_hotends_sent(printer: dict[str, str]) -> None:
    # Checked first, in a dry run too: the dry run rehearses the print's settings, and a
    # bad value fails before the printer is touched.
    try:
        wait = float(WAIT or START_WAIT)
    except ValueError:
        wait = math.nan
    if not (math.isfinite(wait) and wait > 0):
        pytest.fail(f"SCADBUDDY_TEST_PRINTER_WAIT={WAIT!r} is not a positive number of seconds")
    lan = LanPrinter(**printer, cert_sha256=fetch_cert_sha256(printer["host"]))
    # Unique per run, so no earlier job (an earlier probe's among them) carries it.
    name = f"scadbuddy-rack-probe-{int(time.time())}"
    sliced = await lan.download(FILE)
    plan = read_rack_plan(BytesIO(sliced), 1)
    assert plan is not None and plan.on_rack, "the file is no rack plate"
    first = await lan.probe()
    problem = idle_problem(first.print, name)
    assert problem is None, f"not started: {problem}"
    status = status_from_report(row(serial=printer["serial"]), first, now=first.received_at)
    skip = {int(p) for p in SKIP.split(",") if p.strip()}
    chosen = choose_nozzles(
        plan,
        status.model_copy(update={"nozzle_rack": skip_positions(status.nozzle_rack, skip)}),
        algorithm="least_used",
        manual_position=None,
        usage={},
        spools={},
    )
    assert chosen.mapping is not None, f"no pick: {[w.message for w in chosen.warnings]}"
    sent = frozenset(n for n in chosen.mapping if n not in (-1, FIXED_NOZZLE_ID))
    excluded = excluded_hotends(status.nozzle_rack, rack_groups(plan.filaments, {}), sent)
    print(
        f"slot groups {plan.slot_groups}; nozzle_mapping {chosen.mapping}; "
        f"left out {sorted(excluded)}; skipped positions {sorted(skip)}"
    )
    problem = proof_problem(sent, excluded) or fetch_problem(first.print, sent)
    assert problem is None, f"not started: {problem}"
    if DRY:
        pytest.skip("dry run: the pick proves the mapping; nothing was started")
    md5 = await lan.upload(BytesIO(sliced), f"{UPLOAD_DIR}{name}.3mf")
    command = project_file_command(
        ProjectFile(
            url=file_url(f"{name}.3mf"),
            param="Metadata/plate_1.gcode",
            subtask_name=name,
            use_ams=True,
            ams_mapping=[int(t) for t in AMS.split(",")],
            nozzle_mapping=chosen.mapping,
            md5=md5,
            layer_inspect=False,
        ),
        "20011",
    )
    # Read again after the upload: the last report before the command is the baseline.
    baseline = await lan.probe()
    problem = idle_problem(baseline.print, name) or fetch_problem(baseline.print, sent)
    assert problem is None, f"not started: {problem}"
    probe = RackProbe(
        name=name, baseline=baseline.print, sent=sent, excluded=excluded, layer1=LAYER1
    )
    try:
        # The command goes out on the reading connection, so no report after it is missed.
        async with asyncio.timeout(wait):
            async for report in lan.reports(command=command):
                for line in probe.feed(report.print):
                    print(line)
                if probe.done:
                    break
    except TimeoutError:
        pytest.fail(f"not verified within {wait:g} s: {probe.missing()}")
    finally:
        # Only the probe's own job: a refused start leaves whatever ran before untouched.
        if probe.taken:
            await lan.publish({"print": {"command": "stop", "sequence_id": "20012"}})
    assert probe.done, f"the connection closed first: {probe.missing()}"
    print(
        f"rack hotends fetched: {probe.fetched}; left out {sorted(excluded)}; layer {probe.layer}"
    )
    if not LAYER1:
        print("pre-print not watched past the last fetch sent (LAYER1=1 watches it all)")
