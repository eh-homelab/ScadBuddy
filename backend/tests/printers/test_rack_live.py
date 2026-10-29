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
ids) as its ``ams_mapping`` and the pick as its ``nozzle_mapping``. It waits up to
`START_WAIT` for the printer to fetch a rack hotend and print layer 1 of *this* job,
failing on a ``print_error`` or on a rack hotend fetched (``device.nozzle.tar_id``) that
the mapping does not name, then stops it. What counts as this job's is
`tests.printers.rack_probe` (#4933: the first live run passed on the previous job's
``tar_id`` and ``layer_num``). It refuses to start unless the printer is idle
(`rack_probe.idle_problem`): ``IDLE``, ``FINISH`` or ``FAILED``, and no ``print_error``
outside a failed job, and a pick that sends a rack hotend other than the mounted one
(`rack_probe.fetch_problem`). The printer reports no empty-bed flag, so
``SCADBUDDY_TEST_PRINTER_PRINT=1`` is the runner's word that the bed is clear. Whoever
runs it watches the screen: the hotends it picks up are the ones printed (``-s`` shows
the mapping sent, each fetch and each layer).
"""

from __future__ import annotations

import asyncio
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
from tests.conftest import printer_env
from tests.printers.rack_probe import RackProbe, fetch_problem, idle_problem
from tests.printers.test_status import row

pytestmark = pytest.mark.requires_printer

#: How long the printer may take to heat, fetch a rack hotend and print layer 1.
START_WAIT = 1200


@pytest.fixture
def printer() -> dict[str, str]:
    env = printer_env()
    assert env is not None
    return env


@pytest.mark.skipif(
    os.environ.get("SCADBUDDY_TEST_PRINTER_PRINT") != "1"
    or not os.environ.get("SCADBUDDY_TEST_PRINTER_FILE")
    or not os.environ.get("SCADBUDDY_TEST_PRINTER_AMS"),
    reason="starts a print: opt in with SCADBUDDY_TEST_PRINTER_PRINT=1, _FILE and _AMS",
)
async def test_a_rack_print_uses_the_hotends_sent(printer: dict[str, str]) -> None:
    lan = LanPrinter(**printer, cert_sha256=fetch_cert_sha256(printer["host"]))
    # Unique per run, so no earlier job (an earlier probe's among them) carries it.
    name = f"scadbuddy-rack-probe-{int(time.time())}"
    sliced = await lan.download(os.environ["SCADBUDDY_TEST_PRINTER_FILE"])
    plan = read_rack_plan(BytesIO(sliced), 1)
    assert plan is not None and plan.on_rack, "the file is no rack plate"
    first = await lan.probe()
    problem = idle_problem(first.print, name)
    assert problem is None, f"not started: {problem}"
    status = status_from_report(row(serial=printer["serial"]), first, now=first.received_at)
    chosen = choose_nozzles(
        plan, status, algorithm="least_used", manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is not None, f"no pick: {[w.message for w in chosen.warnings]}"
    print(f"slot groups {plan.slot_groups}; nozzle_mapping {chosen.mapping}")
    sent = frozenset(n for n in chosen.mapping if n not in (-1, FIXED_NOZZLE_ID))
    problem = fetch_problem(first.print, sent)
    assert problem is None, f"not started: {problem}; pick a plate that swaps hotends"
    md5 = await lan.upload(BytesIO(sliced), f"{UPLOAD_DIR}{name}.3mf")
    command = project_file_command(
        ProjectFile(
            url=file_url(f"{name}.3mf"),
            param="Metadata/plate_1.gcode",
            subtask_name=name,
            use_ams=True,
            ams_mapping=[int(t) for t in os.environ["SCADBUDDY_TEST_PRINTER_AMS"].split(",")],
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
    probe = RackProbe(name=name, baseline=baseline.print, sent=sent)
    try:
        # The command goes out on the reading connection, so no report after it is missed.
        async with asyncio.timeout(START_WAIT):
            async for report in lan.reports(command=command):
                for line in probe.feed(report.print):
                    print(line)
                if probe.done:
                    break
    except TimeoutError:
        pytest.fail(f"not verified within {START_WAIT} s: {probe.missing()}")
    finally:
        # Only the probe's own job: a refused start leaves whatever ran before untouched.
        if probe.taken:
            await lan.publish({"print": {"command": "stop", "sequence_id": "20012"}})
    assert probe.done, f"the connection closed first: {probe.missing()}"
    print(f"rack hotends fetched: {probe.fetched}; layer {probe.layer}")
