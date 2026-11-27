"""#7170: the settings for printing labels through CUPS (spec
`docs/superpowers/specs/2026-11-27-cups-label-printing-design.md`). Stored only: Go's
`internal/labelprint` reads the rows by name."""

from __future__ import annotations

from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

#: The cluster's own CUPS server.
DEFAULT_SERVER_URL: Final = "http://cups.cups.svc:8631"

PrintRate = Literal["Default", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"]


def server_url(value: str) -> str:
    """The CUPS server's URL, trimmed and with no trailing ``/``: http or https, a host, no
    login, path, query or fragment (an empty ``?`` or ``#`` counts too).
    Anything else is a ``ValueError``."""
    text = value.strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError as error:
        raise ValueError("a label print server is an http or https URL") from error
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or "?" in text
        or "#" in text
        or parts.path not in ("", "/")
    ):
        raise ValueError("a label print server is an http or https URL with a host only")
    return text.rstrip("/")


class LabelJobOptions(BaseModel):
    """What a label job asks of a Zebra ZPL queue. ``Saved`` and ``Default`` leave the
    printer's own setting; ``darkness`` -1 does the same."""

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["Saved", "Thermal", "Direct"] = "Saved"
    darkness: int = -1
    print_rate: PrintRate = "Default"

    @field_validator("darkness")
    @classmethod
    def _darkness(cls, darkness: int) -> int:
        if darkness != -1 and not 1 <= darkness <= 30:
            raise ValueError("darkness is -1 or 1 to 30")
        return darkness
