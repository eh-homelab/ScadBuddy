"""Bambuddy's virtual printers, managed from ScadBuddy (#4747 item 4; plan
2026-10-26-bambuddy-printer-sync, Ruling 8).

Bambuddy's routes are ``/api/v1/virtual-printers`` (list, create), ``/{id}`` (read,
update, delete) and ``/{id}/diagnostic`` (``backend/app/api/routes/virtual_printers.py``,
maziggy/bambuddy v1.3.0b1-daily.20261025). The reads need ``settings:read``, which an API
key holds through Read Status; the writes need ``settings:update``, which no API key can
hold (``backend/app/core/auth.py`` ``_APIKEY_SCOPE_BY_PERMISSION``, lines 79-82 and 130).
So a write works only while Bambuddy's auth is off, as prod runs; otherwise Bambuddy
answers 403, reported as `Scope.ADMIN_SETTINGS`. Bambuddy's own 400s (an invalid mode, a
bind IP another VP uses, a missing target in proxy mode) come back as 422 with its
reason.

The writes are operation kinds on the ``bambuddy`` queue. The access code an untargeted
VP may be given is sealed by the route before the request is recorded
(`seal_access_code`), so it is never in an operation's row or a Temporal payload in the
clear, and opened by the run; the seal is deterministic per VP and code, so a re-sent
request keeps its operation key. A targeted VP inherits its target's code (Bambuddy
overwrites any other, lines 236-240), so a code with a target is refused."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import VirtualPrinter
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek, SealError, open_bytes, seal_bytes
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy
from scadbuddy.printers.models import VirtualPrinterMode
from scadbuddy.printers.store import PrinterStore

#: Bambuddy's bound: "Access code must be exactly 8 characters" (lines 189-190).
ACCESS_CODE_LENGTH = 8
#: The HKDF label of `access_code_key`.
ACCESS_CODE_KEY_INFO = b"scadbuddy:bambuddy:vp-access-code:v1"

CODE_WITH_TARGET = (
    "a virtual printer with a target uses its target's access code; send no access code"
)


class VirtualPrinterView(VirtualPrinter):
    """A virtual printer as ScadBuddy shows it: Bambuddy's values, plus the registry
    row it is (`printer_id`) and its target as a registry printer (`target`)."""

    printer_id: str | None = None
    target: str | None = None


class VirtualPrinterDeleted(BaseModel):
    deleted: int


class VirtualPrintersView(BaseModel):
    printers: list[VirtualPrinterView]
    #: Every model code Bambuddy offers, with its display name.
    models: dict[str, str]
    #: Whether Bambuddy's auth is on (``GET /api/v1/auth/status``); ``None`` unread. On,
    #: no API key can write a virtual printer.
    auth_enabled: bool | None = None


_Host = Field(default=None, max_length=253)


class VirtualPrinterCreate(BaseModel):
    """Bambuddy's ``VirtualPrinterCreate`` (lines 37-49), the target as a registry
    printer. ``access_code`` is write-only and never stored by ScadBuddy."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    mode: VirtualPrinterMode = "archive"
    model: str | None = Field(default=None, max_length=16)
    access_code: str | None = Field(
        default=None,
        min_length=ACCESS_CODE_LENGTH,
        max_length=ACCESS_CODE_LENGTH,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        repr=False,
    )
    #: A registry printer's id; ``None`` for a model-based VP ("any <model>").
    target: str | None = None
    auto_dispatch: bool = True
    queue_force_color_match: bool = False
    save_ams_mapping: bool = False
    gcode_injection: bool = False
    bind_ip: str | None = _Host
    remote_interface_ip: str | None = _Host


class VirtualPrinterUpdate(BaseModel):
    """Bambuddy's ``VirtualPrinterUpdate`` (lines 52-66): only what is sent changes.
    ``target`` sent as ``null`` takes the target off."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    mode: VirtualPrinterMode | None = None
    model: str | None = Field(default=None, max_length=16)
    access_code: str | None = Field(
        default=None,
        min_length=ACCESS_CODE_LENGTH,
        max_length=ACCESS_CODE_LENGTH,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        repr=False,
    )
    target: str | None = None
    auto_dispatch: bool | None = None
    queue_force_color_match: bool | None = None
    save_ams_mapping: bool | None = None
    gcode_injection: bool | None = None
    bind_ip: str | None = _Host
    remote_interface_ip: str | None = _Host
    tailscale_disabled: bool | None = None


# ── the access code's seal ───────────────────────────────────────────────────────


def access_code_key(kek: Kek) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=ACCESS_CODE_KEY_INFO).derive(
        kek.key
    )


def _context(subject: str) -> str:
    return f"vp-access-code:{subject}"


def seal_access_code(kek: Kek, code: str, subject: str) -> str:
    """The code sealed for the operation's request: AES-GCM under a key derived from
    the KEK, its IV the HMAC of the subject and code (a synthetic IV, so the same code
    for the same VP seals the same, and a re-sent request keeps its operation key)."""
    key = access_code_key(kek)
    iv = hmac.new(key, f"{subject}\0{code}".encode(), hashlib.sha256).digest()[:12]
    return base64.b64encode(seal_bytes(key, code.encode(), _context(subject), iv=iv)).decode()


def open_access_code(kek: Kek, sealed: str, subject: str) -> str:
    return open_bytes(access_code_key(kek), base64.b64decode(sealed), _context(subject)).decode()


# ── the operation kinds ──────────────────────────────────────────────────────────


def virtual_printer_kinds(
    *, settings_store: SettingsStore, printers: PrinterStore | None
) -> list[OperationKind]:
    """``bambuddy_vp_create``, ``bambuddy_vp_update`` and ``bambuddy_vp_delete``. Each
    runs once: Bambuddy dedupes none of them (a second create makes a second VP)."""

    async def target_id(request: dict[str, Any]) -> int | None:
        target = request.get("target")
        if target is None:
            return None
        row = None if printers is None else await asyncio.to_thread(printers.get, target)
        if row is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no printer {target}")
        if row.bambuddy_printer_id is None:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{row.name} is not linked to a Bambuddy printer, so a virtual printer"
                " cannot forward to it",
            )
        return row.bambuddy_printer_id

    async def write_check(request: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = request.get("fields", {})
        checked: dict[str, Any] = {}
        if "target" in fields:
            checked["target_printer_id"] = await target_id(fields)
            if checked["target_printer_id"] is not None and request.get("access_code"):
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, CODE_WITH_TARGET)
        if request.get("access_code") and (printers is None or printers.kek is None):
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "a virtual printer's access code needs SCADBUDDY_SECRET_KEY_FILE on the"
                " print worker",
            )
        return checked

    def body_of(request: dict[str, Any], checked: dict[str, Any], subject: str) -> dict[str, Any]:
        fields = {k: v for k, v in request.get("fields", {}).items() if k != "target"}
        if "target_printer_id" in checked:
            fields["target_printer_id"] = checked["target_printer_id"]
        sealed = request.get("access_code")
        if sealed:
            assert printers is not None and printers.kek is not None
            try:
                fields["access_code"] = open_access_code(printers.kek, sealed, subject)
            except SealError:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "the access code was sealed under another key; send it again",
                ) from None
        return fields

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = body_of(request, checked, "new")
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            created = await client.create_virtual_printer(body)
        return created.model_dump(mode="json")

    async def update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        vp_id = int(request["vp_id"])
        body = body_of(request, checked, str(vp_id))
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            updated = await client.update_virtual_printer(vp_id, body)
        return updated.model_dump(mode="json")

    async def delete_check(request: dict[str, Any]) -> dict[str, Any]:
        settings = await asyncio.to_thread(settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            await client.virtual_printer(int(request["vp_id"]))
        return {}

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        vp_id = int(request["vp_id"])
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            await client.delete_virtual_printer(vp_id)
        return {"deleted": vp_id}

    where = "Bambuddy's Settings > Virtual Printer"
    return [
        OperationKind("bambuddy_vp_create", write_check, create_run, where=where),
        OperationKind("bambuddy_vp_update", write_check, update_run, where=where),
        OperationKind("bambuddy_vp_delete", delete_check, delete_run, where=where),
    ]
