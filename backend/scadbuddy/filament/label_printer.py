"""#7170: the network label printer's settings (spec
`docs/superpowers/specs/2026-11-27-zpl-label-printer-design.md`). Stored only: Go's
`internal/labelprinter` reads the four rows by name."""

from __future__ import annotations

import ipaddress
import re
from typing import Final, Literal

MediaType = Literal["direct", "transfer"]

#: The raw ZPL port of a Zebra printer.
DEFAULT_PORT: Final = 9100
#: The GK420t's own darkness (`~SD`).
DEFAULT_DARKNESS: Final = 10

_MAX_HOST: Final = 253
_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def printer_host(value: str) -> str:
    """The host, trimmed: a DNS name or an IP address (IPv6 with no brackets). Anything
    else is a ``ValueError``."""
    host = value.strip()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    if not host or len(host) > _MAX_HOST:
        raise ValueError("a label printer host is a DNS name or an IP address")
    if not all(_LABEL.fullmatch(label) for label in host.split(".")):
        raise ValueError("a label printer host is a DNS name or an IP address")
    return host
