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
ids) as its ``ams_mapping`` and the pick as its ``nozzle_mapping``. It waits for the
printer to run it, failing on a ``print_error`` or on a rack hotend fetched
(``device.nozzle.tar_id``) that the mapping does not name, then stops it. Whoever runs it
watches the screen: the hotends it picks up are the ones printed (``-s`` shows the
mapping sent).
"""

from __future__ import annotations

import asyncio
import os
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
from tests.printers.test_status import row

pytestmark = pytest.mark.requires_printer

NAME = "scadbuddy-rack-probe"
#: How long the printer may take to fetch its hotends and start printing.
START_WAIT = 300


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
    sliced = await lan.download(os.environ["SCADBUDDY_TEST_PRINTER_FILE"])
    plan = read_rack_plan(BytesIO(sliced), 1)
    assert plan is not None and plan.on_rack, "the file is no rack plate"
    first = await lan.probe()
    status = status_from_report(row(serial=printer["serial"]), first, now=first.received_at)
    chosen = choose_nozzles(
        plan, status, algorithm="least_used", manual_position=None, usage={}, spools={}
    )
    assert chosen.mapping is not None, f"no pick: {[w.message for w in chosen.warnings]}"
    print(f"slot groups {plan.slot_groups}; nozzle_mapping {chosen.mapping}")
    sent = {nozzle for nozzle in chosen.mapping if nozzle not in (-1, FIXED_NOZZLE_ID)}
    md5 = await lan.upload(BytesIO(sliced), f"{UPLOAD_DIR}{NAME}.3mf")
    command = project_file_command(
        ProjectFile(
            url=file_url(f"{NAME}.3mf"),
            param="Metadata/plate_1.gcode",
            subtask_name=NAME,
            use_ams=True,
            ams_mapping=[int(t) for t in os.environ["SCADBUDDY_TEST_PRINTER_AMS"].split(",")],
            nozzle_mapping=chosen.mapping,
            md5=md5,
            layer_inspect=False,
        ),
        "20011",
    )
    ours = False
    fetched: list[int] = []
    try:
        await lan.publish(command)
        async with asyncio.timeout(START_WAIT):
            async for report in lan.reports():
                data = report.print
                if data.get("subtask_name") != NAME:
                    continue
                ours = True
                assert data.get("print_error", 0) == 0
                nozzle = (data.get("device") or {}).get("nozzle") or {}
                target = nozzle.get("tar_id")
                if isinstance(target, int) and target >= 16 and target not in fetched:
                    fetched.append(target)
                    print(f"fetching rack hotend {target}")
                    assert target in sent, f"fetched {target}, sent {sorted(sent)}"
                if data.get("gcode_state") == "RUNNING" and (data.get("layer_num") or 0) >= 1:
                    break
    finally:
        # Only the probe's own job: a refused start leaves whatever ran before untouched.
        if ours:
            await lan.publish({"print": {"command": "stop", "sequence_id": "20012"}})
    print(f"rack hotends fetched: {fetched}")
