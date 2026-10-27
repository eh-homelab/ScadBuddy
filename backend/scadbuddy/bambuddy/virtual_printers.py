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
(`seal_access_code`, a random IV, bound to the VP's id or, for a create, to the request's
Idempotency-Key), so it is never in an operation's row or a Temporal payload in the
clear, and opened by the run. The sealed value is left out of the operation key
(`OperationKind.unkeyed`); in its place the key covers `access_code_mac`, a MAC of the
code under its own KEK-derived key and bound to the Idempotency-Key, so a re-send with
the same key and code finds its record while equal codes in different requests never
look equal. A targeted VP inherits its target's code (Bambuddy overwrites any other,
lines 236-240), so a code with a target is refused."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import VirtualPrinter
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek, SealError, open_bytes, seal_bytes
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy
from scadbuddy.printers.bambuddy_sync import StoreRegistry, sync_virtual_printers
from scadbuddy.printers.models import VirtualPrinterMode
from scadbuddy.printers.store import PrinterStore

logger = logging.getLogger(__name__)

#: Bambuddy's bound: "Access code must be exactly 8 characters" (lines 189-190).
ACCESS_CODE_LENGTH = 8
#: The HKDF labels of `access_code_key` and of `access_code_mac`'s key.
ACCESS_CODE_KEY_INFO = b"scadbuddy:bambuddy:vp-access-code:v1"
ACCESS_CODE_MAC_INFO = b"scadbuddy:bambuddy:vp-access-code-mac:v1"
#: The request fields `access_code_request` writes; the sealed one is unkeyed.
SEALED_FIELD = "access_code"
MAC_FIELD = "access_code_mac"
CONTEXT_FIELD = "seal_context"

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


def _derived(kek: Kek, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(kek.key)


def access_code_key(kek: Kek) -> bytes:
    return _derived(kek, ACCESS_CODE_KEY_INFO)


def seal_context(*, vp_id: int | None, idempotency_key: str) -> str:
    """What a seal is bound to: the VP's id, or for a create (no id yet) the request's
    own Idempotency-Key, which no other request carries."""
    return (
        f"vp-access-code:vp:{vp_id}"
        if vp_id is not None
        else (f"vp-access-code:new:{idempotency_key}")
    )


def seal_access_code(kek: Kek, code: str, context: str) -> str:
    """The code sealed for the operation's request: AES-GCM under a key derived from
    the KEK, a random IV, ``context`` as the AAD."""
    return seal_bytes(access_code_key(kek), code.encode(), context).hex()


def open_access_code(kek: Kek, sealed: str, context: str) -> str:
    return open_bytes(access_code_key(kek), bytes.fromhex(sealed), context).decode()


def access_code_mac(kek: Kek, code: str, context: str, idempotency_key: str) -> str:
    """The code's stand-in in the operation key: an HMAC under its own KEK-derived key,
    over the request's Idempotency-Key, so only a re-send of the same request matches."""
    message = f"{context}\0{idempotency_key}\0{code}".encode()
    return hmac.new(_derived(kek, ACCESS_CODE_MAC_INFO), message, hashlib.sha256).hexdigest()


def access_code_request(
    kek: Kek, code: str, *, vp_id: int | None, idempotency_key: str
) -> dict[str, str]:
    """The request fields that carry ``code``: sealed, its MAC and the seal's context."""
    context = seal_context(vp_id=vp_id, idempotency_key=idempotency_key)
    return {
        SEALED_FIELD: seal_access_code(kek, code, context),
        MAC_FIELD: access_code_mac(kek, code, context, idempotency_key),
        CONTEXT_FIELD: context,
    }


# ── the operation kinds ──────────────────────────────────────────────────────────

UNKEYED = frozenset({SEALED_FIELD})


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
            if checked["target_printer_id"] is not None and request.get(SEALED_FIELD):
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, CODE_WITH_TARGET)
        if request.get(SEALED_FIELD) and "vp_id" in request and "target" not in fields:
            # An update that keeps the target: Bambuddy would replace a code sent to a
            # targeted VP with its target's (lines 236-240), so it is refused as a create's.
            settings = await asyncio.to_thread(settings_store.load)
            async with waiting_on_bambuddy(), client_for(settings) as client:
                current = await client.virtual_printer(int(request["vp_id"]))
            if current.target_printer_id is not None:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, CODE_WITH_TARGET)
        if request.get(SEALED_FIELD) and (printers is None or printers.kek is None):
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "a virtual printer's access code needs SCADBUDDY_SECRET_KEY_FILE on the"
                " print worker",
            )
        return checked

    async def follow(client: BambuddyClient) -> None:
        """The registry follows the write, here on the worker that made it, so a write
        that outlives the route's deadline is followed too. Best effort: the write is
        done whatever this says, and the daily sweep is the backstop."""
        if printers is None:
            return
        try:
            await sync_virtual_printers(StoreRegistry(printers), client)
        except Exception:
            logger.warning("could not sync the virtual printers after a write")

    def body_of(
        request: dict[str, Any], checked: dict[str, Any], vp_id: int | None
    ) -> dict[str, Any]:
        fields = {k: v for k, v in request.get("fields", {}).items() if k != "target"}
        if "target_printer_id" in checked:
            fields["target_printer_id"] = checked["target_printer_id"]
        sealed = request.get(SEALED_FIELD)
        if sealed:
            assert printers is not None and printers.kek is not None
            context = str(request.get(CONTEXT_FIELD, ""))
            # A create's seal names its request; an update's must name this VP.
            expected = "vp-access-code:new:" if vp_id is None else f"vp-access-code:vp:{vp_id}"
            if not (context.startswith(expected) if vp_id is None else context == expected):
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "the access code was sealed for another virtual printer",
                )
            try:
                fields["access_code"] = open_access_code(printers.kek, sealed, context)
            except SealError:
                raise ApiError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "the access code was sealed under another key; send it again",
                ) from None
        return fields

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = body_of(request, checked, None)
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            created = await client.create_virtual_printer(body)
            await follow(client)
        return created.model_dump(mode="json")

    async def update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        vp_id = int(request["vp_id"])
        body = body_of(request, checked, vp_id)
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            updated = await client.update_virtual_printer(vp_id, body)
            await follow(client)
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
            await follow(client)
        return {"deleted": vp_id}

    where = "Bambuddy's Settings > Virtual Printer"
    return [
        OperationKind("bambuddy_vp_create", write_check, create_run, where=where, unkeyed=UNKEYED),
        OperationKind("bambuddy_vp_update", write_check, update_run, where=where, unkeyed=UNKEYED),
        OperationKind("bambuddy_vp_delete", delete_check, delete_run, where=where),
    ]
