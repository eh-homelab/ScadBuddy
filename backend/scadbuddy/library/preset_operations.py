"""Preset writes that validate against the template as operations (#1054, spec
2026-10-01 §4.3, the ``library`` row: "preset writes that need ``openscad``"): create,
duplicate and update. The check makes the model's 404 and a shipped preset's 403; the
run validates the values with openscad (422, or 503 without it) and writes Postgres.
A delete runs no openscad and stays a plain route (plan 3e Ruling 3).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from scadbuddy.api import presets as presets_api
from scadbuddy.api.models import require_model_exists
from scadbuddy.library.operations import answered_as_routes
from scadbuddy.library.presets import (
    ParamPreset,
    ParamPresetCreate,
    ParamPresetDuplicate,
    ParamPresetUpdate,
)
from scadbuddy.operations.kinds import OperationKind

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState


def preset_kinds(state: AppState) -> list[OperationKind]:
    """The preset kinds, bound to this process's state; ``library/operations.py``
    exports them with the pins."""

    def _preset(preset: ParamPreset) -> dict[str, Any]:
        dumped: dict[str, Any] = preset.model_dump(mode="json")
        return dumped

    async def exists_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        return {}

    async def update_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        await asyncio.to_thread(
            presets_api.require_saved, state.presets, request["slug"], request["preset_id"]
        )
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = ParamPresetCreate.model_validate(request["body"])
        return _preset(await presets_api.create_run(request["slug"], body, state))

    async def duplicate_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = ParamPresetDuplicate.model_validate(request["body"])
        preset = await presets_api.duplicate_run(request["slug"], request["preset_id"], body, state)
        return _preset(preset)

    async def update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = ParamPresetUpdate.model_validate(request["body"])
        preset = await presets_api.update_run(request["slug"], request["preset_id"], body, state)
        return _preset(preset)

    def kind(name: str, check: Any, run: Any) -> OperationKind:
        return OperationKind(
            name,
            answered_as_routes(check),
            answered_as_routes(run),
            queue="library",
            where="the template's presets",
        )

    return [
        kind("preset_create", exists_check, create_run),
        kind("preset_duplicate", exists_check, duplicate_run),
        kind("preset_update", update_check, update_run),
    ]
