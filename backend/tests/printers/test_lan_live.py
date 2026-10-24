"""The LAN client against a real printer (#3823, plan 2026-10-16, Tasks 4 and 7). Skipped
unless `tests.conftest.PRINTER_ENV` names one; run by hand, never in CI.

Task 7's measurement, on an H2C (firmware OTA 01.02.00.00, AHB 02.04.11.98, MC
03.00.81.69) on 2026-10-24, with every port of the printer reachable from the bench:

- 8883 and 990 present one leaf certificate (`test_mqtt_and_ftps_present_one_certificate`).
- `test_upload_then_project_file_is_accepted`, which *starts a print* and so runs only
  with ``SCADBUDDY_TEST_PRINTER_PRINT=1`` and an empty bed: a 774,738-byte sliced
  six-colour 3MF stored at ``/scadbuddy-probe.3mf`` and named ``ftp://scadbuddy-probe.3mf``,
  ``ams_mapping`` [4, 1, 6, 12, 5, 13], ids ``"0"``, no ``ams_mapping2`` or
  ``nozzle_mapping``, was answered ``result: SUCCESS`` in 0.4 s and was ``RUNNING`` at
  1.8 s; the ``stop`` sent then left it ``FAILED``. So `lan.UPLOAD_DIR` is ``/`` and
  `lan.file_url` is ``ftp://<name>``.
- While idle the printer pushed a whole ``push_status`` (about 19 KB) every ~1.8 s,
  never a partial one, and messages could arrive out of timestamp order: the merge keys
  on the latest, never the sequence (`tests/printers/fixtures/lan-tray-reinsert-h2c.json`).
"""

from __future__ import annotations

import asyncio
import os
import zipfile
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
from tests.conftest import printer_env

pytestmark = pytest.mark.requires_printer


@pytest.fixture
def printer() -> dict[str, str]:
    env = printer_env()
    assert env is not None
    return env


async def test_probe_returns_gcode_state(printer: dict[str, str]) -> None:
    report = await LanPrinter(**printer, cert_sha256=fetch_cert_sha256(printer["host"])).probe()
    assert "gcode_state" in report.print


def test_mqtt_and_ftps_present_one_certificate(printer: dict[str, str]) -> None:
    """#4186 review M9, for the Task 7 printer session: the pin read on 8883 is enforced
    on 990 too, so both ports must present the same leaf certificate."""
    from scadbuddy.printers.lan import FTPS_PORT, MQTT_PORT

    assert fetch_cert_sha256(printer["host"], MQTT_PORT) == fetch_cert_sha256(
        printer["host"], FTPS_PORT
    )


@pytest.mark.skipif(
    os.environ.get("SCADBUDDY_TEST_PRINTER_PRINT") != "1"
    or not os.environ.get("SCADBUDDY_TEST_PRINTER_FILE")
    or not os.environ.get("SCADBUDDY_TEST_PRINTER_AMS"),
    reason="starts a print: opt in with SCADBUDDY_TEST_PRINTER_PRINT=1, _FILE and _AMS",
)
async def test_upload_then_project_file_is_accepted(printer: dict[str, str]) -> None:
    """Stores a sliced 3MF the printer already holds under a probe name (set
    ``SCADBUDDY_TEST_PRINTER_FILE`` to its path there, e.g. ``/cache/x.gcode.3mf``),
    starts it with ``SCADBUDDY_TEST_PRINTER_AMS`` (comma-separated tray ids) as its
    ``ams_mapping``, and stops it as soon as the printer is running it."""
    lan = LanPrinter(**printer, cert_sha256=fetch_cert_sha256(printer["host"]))
    sliced = await lan.download(os.environ["SCADBUDDY_TEST_PRINTER_FILE"])
    assert "Metadata/plate_1.gcode" in zipfile.ZipFile(BytesIO(sliced)).namelist()
    name = "scadbuddy-probe.3mf"
    md5 = await lan.upload(BytesIO(sliced), f"{UPLOAD_DIR}{name}")
    command = project_file_command(
        ProjectFile(
            url=file_url(name),
            param="Metadata/plate_1.gcode",
            subtask_name="scadbuddy-probe",
            use_ams=True,
            ams_mapping=[int(t) for t in os.environ["SCADBUDDY_TEST_PRINTER_AMS"].split(",")],
            md5=md5,
            layer_inspect=False,
        ),
        "20001",
    )
    try:
        await lan.publish(command)
        async with asyncio.timeout(60):
            async for report in lan.reports():
                if report.print.get("subtask_name") != "scadbuddy-probe":
                    continue
                assert report.print.get("print_error", 0) == 0
                if report.print.get("gcode_state") in ("PREPARE", "RUNNING"):
                    break
    finally:
        await lan.publish({"print": {"command": "stop", "sequence_id": "20002"}})
