"""The printer registry's shapes (#3823, plan 2026-10-16-printer-identity-native-path,
Task 2). A ``PrinterRow`` never carries the access code: only whether one is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.printers.address import valid_host

#: How a run reaches the printer: through Bambuddy, over the LAN, or ``auto`` (the LAN
#: when the printer is reachable there, else Bambuddy).
PrintPath = Literal["bambuddy", "native", "auto"]

#: What a registry row is (#4747): a printer, or one of Bambuddy's virtual printers,
#: which is never counted as hardware (not in the strip, the dialog or the hot path).
PrinterKind = Literal["physical", "bambuddy_virtual"]

#: A virtual printer's mode, Bambuddy's canonical values (``VP_MODE_VALUES`` in
#: ``backend/app/models/virtual_printer.py`` at maziggy/bambuddy v1.3.0b1-daily.20261025;
#: ``immediate`` is its alias for ``archive``).
VirtualPrinterMode = Literal["archive", "review", "queue", "proxy"]

#: The serial a Bambuddy printer gets when Bambuddy withholds its own: one row per
#: Bambuddy printer all the same. Such a row cannot take a LAN link (the MQTT topics are
#: named by the real serial).
SYNTHETIC_SERIAL_PREFIX = "bambuddy-"

#: The longest access code a LAN link takes, in code points. Settings > Printers'
#: `ACCESS_CODE_MAX_LENGTH` (frontend/src/components/settings/Printers.tsx) is the same
#: number, and its test reads this one from the API spec, so the two move together.
ACCESS_CODE_MAX_LENGTH = 128


def synthetic_serial(bambuddy_printer_id: int) -> str:
    return f"{SYNTHETIC_SERIAL_PREFIX}{bambuddy_printer_id}"


def synthetic_vp_serial(vp_id: int) -> str:
    """A virtual printer's row serial when its own is taken (a proxy VP reports its
    target's) or is not a serial's shape: synthetic, so it never takes a LAN link."""
    return f"{SYNTHETIC_SERIAL_PREFIX}vp-{vp_id}"


def is_synthetic_serial(serial: str) -> bool:
    return serial.startswith(SYNTHETIC_SERIAL_PREFIX)


class BambuddyView(BaseModel):
    """What Bambuddy reported for a row at the last sync (#4747 item 1), shown beside
    the row's own values. Never an access code: only whether a virtual printer has one."""

    #: When the sync first saw these values (a sync that finds nothing changed writes
    #: nothing, so this is when Bambuddy last reported a change).
    seen_at: datetime
    name: str
    model: str | None = None
    #: A printer's address as Bambuddy has it; a virtual printer has none Bambuddy knows.
    ip_address: str | None = None
    is_active: bool | None = None
    nozzle_count: int | None = None
    # A virtual printer's settings, as Bambuddy reports them.
    enabled: bool | None = None
    mode: str | None = None
    model_code: str | None = None
    target_printer_id: int | None = None
    bind_ip: str | None = None
    remote_interface_ip: str | None = None
    access_code_set: bool | None = None
    running: bool | None = None
    pending_files: int | None = None


class PrinterRow(BaseModel):
    id: str
    serial: str
    name: str
    model: str | None = None
    bambuddy_printer_id: int | None = None
    #: The Bambuddy printer last unlinked from this row; `ensure_bambuddy` never links
    #: it again by serial (only `link_bambuddy` does, and clears this).
    bambuddy_unlinked_id: int | None = None
    lan_host: str | None = None
    #: The pinned leaf certificate's SHA-256 (hex), recorded by the LAN link's probe.
    lan_cert_sha256: str | None = None
    has_access_code: bool = False
    preferred_path: PrintPath = "auto"
    #: Whether Bambuddy listed its printer as active when it last answered: what the
    #: strip shows while Bambuddy is down.
    bambuddy_active: bool = True
    kind: PrinterKind = "physical"
    #: The Bambuddy virtual printer this row is (``kind`` ``bambuddy_virtual``).
    bambuddy_vp_id: int | None = None
    #: The registry printer a virtual printer forwards to; ``None`` for one Bambuddy
    #: dispatches to any free printer of its model.
    forwards_to: str | None = None
    vp_mode: str | None = None
    bambuddy_view: BambuddyView | None = None
    #: Why the Bambuddy sync last could not fill or move the LAN link; never the code.
    lan_sync_error: str | None = None
    created_at: datetime
    updated_at: datetime


class PrinterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial: str = Field(min_length=8, max_length=32, pattern=r"^[0-9A-Z]+$")
    name: str = Field(min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=40)
    preferred_path: PrintPath = "auto"


class PrinterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    preferred_path: PrintPath | None = None


class BambuddyLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bambuddy_printer_id: int = Field(ge=1)


def _host(value: str) -> str:
    if not valid_host(value):
        raise ValueError("an IP address or a host name, with no scheme, port or path")
    return value


class LanLink(BaseModel):
    """What the user types to link a printer over the LAN. ``access_code`` is kept out
    of ``repr`` and never stored in the clear."""

    model_config = ConfigDict(extra="forbid")

    #: An IP literal or an RFC 1123 host name; where it may point is checked at every
    #: connect (`address.lan_address`).
    host: Annotated[str, AfterValidator(_host)] = Field(min_length=1, max_length=253)
    #: As typed, in no format (#4434): the printer decides whether it is right, and the
    #: link's probe asks it. The bound only keeps a pasted page out, and no control
    #: character, which ftplib refuses in the FTPS login (`lan.py`), gets past the link.
    access_code: str = Field(
        min_length=1,
        max_length=ACCESS_CODE_MAX_LENGTH,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        repr=False,
    )
