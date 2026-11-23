"""The SpoolBuddy scale's tare through Bambuddy (#7155, plan
`docs/superpowers/plans/2026-11-23-spoolbuddy-tare.md`). Bambuddy keeps one pending command
per device and a tare replaces it, so `tare` reads the device first and refuses a device
Bambuddy does not list, one with no scale, and one with another command pending (R-7155-4).
The device list and the tare are two calls, so a command queued between them is still
replaced; a person sees the device list before pressing the button."""

from __future__ import annotations

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.filament.models import ScaleTareQueued


class ScaleRefusedError(ValueError):
    """A tare ScadBuddy does not send; ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def tare(client: BambuddyClient, device_id: str) -> ScaleTareQueued:
    """Queue a tare on ``device_id``."""
    device = next((d for d in await client.spoolbuddy_devices() if d.device_id == device_id), None)
    if device is None:
        raise ScaleRefusedError("no_device", f"Bambuddy lists no SpoolBuddy device {device_id}")
    if not device.has_scale:
        raise ScaleRefusedError("no_scale", f"the SpoolBuddy device {device_id} has no scale")
    pending = device.pending_command
    if pending is not None and pending != "tare":
        raise ScaleRefusedError(
            "device_busy",
            f"the SpoolBuddy device {device_id} has the command {pending} pending; a tare would"
            " replace it, so wait until it is done",
        )
    await client.tare_scale(device_id)
    return ScaleTareQueued(device_id=device_id)
