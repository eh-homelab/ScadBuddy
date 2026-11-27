"""#7170: the label printer's host is a DNS name or an IP address, nothing more."""

from __future__ import annotations

import pytest

from scadbuddy.filament.label_printer import printer_host


def test_a_host_is_a_name_or_an_ip() -> None:
    assert printer_host("zebra.lan") == "zebra.lan"
    assert printer_host(" 192.168.1.40 ") == "192.168.1.40"
    assert printer_host("fe80::1") == "fe80::1"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://zebra",
        "zebra:9100",
        "zebra/x",
        "a@zebra",
        "-zebra",
        "zebra lan",
        "[fe80::1]",
        "999.999.999.999",
        "1234",
        "zebra.123",
        "fe80::1%eth0",
        ("a" * 63 + ".") * 4 + "a",
    ],
)
def test_a_host_with_more_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        printer_host(value)
