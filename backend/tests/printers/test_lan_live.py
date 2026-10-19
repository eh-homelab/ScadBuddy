"""The LAN client against a real printer (#3823, plan 2026-10-16, Task 4). Skipped
unless `tests.conftest.PRINTER_ENV` names one; run by hand, never in CI."""

from __future__ import annotations

import pytest

from scadbuddy.printers.lan import LanPrinter, fetch_cert_sha256
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
