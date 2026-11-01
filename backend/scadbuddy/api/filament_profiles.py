"""``/api/v1/print/filament-profiles`` — filament profiles (#717): named, cited process
changes the print dialog suggests for a spool, such as Silk PLA's slower outer wall.

Read only: listing them reads nothing from Bambuddy, and a preview reads the print's
process preset values (``Manage Library``) to show each change against the value it
replaces. Applying one is the print request's ``filament_profiles``.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filament_profiles import (
    BUILTIN,
    FilamentProfile,
    ProfilePreview,
    previews,
    unknown_profiles,
)
from scadbuddy.bambuddy.resolver import PrintChoices, process_preset_for
from scadbuddy.slicing.catalogue import catalogue_for

router = APIRouter(prefix="/print/filament-profiles", tags=["print"])


class ProfilePreviewRequest(BaseModel):
    #: The print dialog's choices, which decide the process preset.
    choices: PrintChoices
    #: The profiles to preview; omitted, every profile.
    profiles: list[str] | None = Field(default=None, max_length=8)

    @field_validator("profiles")
    @classmethod
    def _known(cls, ids: list[str] | None) -> list[str] | None:
        unknown = unknown_profiles(ids or [])
        if unknown:
            raise ValueError(f"no filament profile {', '.join(map(repr, unknown))}")
        # A repeated id is previewed once, as PrintRunRequest applies it once (#2804 review).
        return None if ids is None else list(dict.fromkeys(ids))


@router.get("", response_model=list[FilamentProfile], summary="Every filament profile")
async def list_profiles() -> list[FilamentProfile]:
    """Every filament profile: which spools it is suggested for (``match``), the process
    settings it changes, each with its cited sources, and the advice it gives but
    cannot apply (``notes``)."""
    return list(BUILTIN.values())


@router.post(
    "/preview",
    response_model=list[ProfilePreview],
    summary="What each profile would change on this print",
)
async def preview_profiles(
    body: ProfilePreviewRequest, store: SettingsStoreDep
) -> list[ProfilePreview]:
    """Each profile's changes against the process preset these choices resolve to: the
    preset's value (a list for a per-extruder-variant option) and the profile's. A
    profile that cannot be applied says why (``applicable: false``, ``reason``), and a
    print with it on goes ahead without it, saying so in its warnings. Its filament
    settings (a nozzle temperature) apply only on the native slicer (``filament_applied``,
    ``filament_reason``, #2799)."""
    settings = store.load()
    async with client_for(settings) as client:
        process = process_preset_for(body.choices, await catalogue_for(client, native=None))
        ids = body.profiles if body.profiles is not None else list(BUILTIN)
        return await previews(
            client,
            process,
            [BUILTIN[i] for i in ids],
            native_slicer=settings.slicer_backend == "native",
        )
