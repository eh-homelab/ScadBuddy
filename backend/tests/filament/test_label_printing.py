"""#7170: the CUPS server's URL is http or https with a host, and the job options have bounds."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from scadbuddy.filament.label_printing import LabelJobOptions, PrintRate, server_url


@pytest.mark.parametrize(
    "value",
    ["http://cups.cups.svc:8631", "https://cups.internal.nullreference.io", "http://10.0.0.5:631/"],
)
def test_a_server_url_is_http_with_a_host(value: str) -> None:
    assert server_url(value) == value.rstrip("/")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "cups:631",
        "ftp://cups",
        "http://",
        "http://u:p@cups",
        "http://cups/printers",
        "http://cups?x=1",
        "http://cups#f",
        "http://cups?",
        "http://cups#",
        "http://cups/?",
        "javascript:alert(1)",
    ],
)
def test_other_values_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        server_url(value)


@pytest.mark.parametrize("darkness", [0, 31, -2])
def test_darkness_is_minus_one_or_1_to_30(darkness: int) -> None:
    with pytest.raises(ValidationError):
        LabelJobOptions(darkness=darkness)


@pytest.mark.parametrize("darkness", [-1, 1, 30])
def test_darkness_values_that_are_allowed(darkness: int) -> None:
    assert LabelJobOptions(darkness=darkness).darkness == darkness


@pytest.mark.parametrize("print_rate", get_args(PrintRate))
def test_every_print_rate_is_allowed(print_rate: str) -> None:
    assert LabelJobOptions(print_rate=print_rate).print_rate == print_rate  # type: ignore[arg-type]
