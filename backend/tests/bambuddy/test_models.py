"""Bambuddy's ``Printer`` carries its serial and address (#3823, plan 2026-10-16
printer-identity, Task 1): what links a Bambuddy printer to the registry by serial."""

from __future__ import annotations

from scadbuddy.bambuddy.models import Printer
from tests.bambuddy.conftest import recording


def test_printer_parses_serial_and_address() -> None:
    rows = recording("printers.json")
    printer = Printer.model_validate(rows[0])
    assert printer.serial_number == "REDACTED-SERIAL"
    assert printer.ip_address == rows[0]["ip_address"]


def test_one_printer_route_parses_the_same_fields() -> None:
    body = recording("printer.json")
    printer = Printer.model_validate(body)
    assert printer.serial_number == "REDACTED"
    assert printer.ip_address == body["ip_address"]


def test_printer_without_serial_is_not_linkable() -> None:
    for withheld in ("", "   ", None):
        printer = Printer.model_validate({"id": 3, "name": "x", "serial_number": withheld})
        assert printer.serial_number is None
    assert Printer.model_validate({"id": 3, "name": "x"}).serial_number is None


def test_a_serial_never_reaches_the_repr() -> None:
    printer = Printer.model_validate({"id": 3, "name": "x", "serial_number": "01P00A000000001"})
    assert "01P00A000000001" not in repr(printer)
